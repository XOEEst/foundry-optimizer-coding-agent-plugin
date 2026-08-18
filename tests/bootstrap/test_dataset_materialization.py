"""Real dataset materialization: SAS download, stable ids, split upload, cleanup.

The adapter's default live path is exercised here: `datasets.get_credentials` -> bounded HTTP
download from the returned SAS uri -> deterministic split -> `datasets.upload_file`. Content is
served by a loopback HTTP server; no Azure, GitHub, or Foundry call ever leaves the machine.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

from foundry_opt.bootstrap.contracts import BootstrapPlan
from foundry_opt.bootstrap.providers import foundry as foundry_module
from foundry_opt.bootstrap.providers.foundry import (
    FoundryNetworkError,
    FoundryPermissionError,
    FoundryPrerequisiteError,
    FoundryUnsupportedCapabilityError,
)
from tests.bootstrap.fakes.evaluation_contract import build_contract
from tests.bootstrap.fakes.foundry_env import SdkValue, build_fake_adapter

RUNTIME_SHA = "a" * 40
SOURCE_NAME = "dev-set-source"
SOURCE_VERSION = "1"
SOURCE_ID = f"azureai://accounts/example/projects/example/data/{SOURCE_NAME}/versions/{SOURCE_VERSION}"


class _SasCredential:
    """Shaped like `BlobReferenceSasCredential`."""

    def __init__(self, sas_uri: str) -> None:
        self.sas_uri = sas_uri
        self.type = "SAS"


class _BlobReference:
    """Shaped like `BlobReference`."""

    def __init__(self, blob_uri: str, sas_uri: str) -> None:
        self.blob_uri = blob_uri
        self.credential = _SasCredential(sas_uri)


class _DatasetCredential:
    """Shaped like `DatasetCredential`."""

    def __init__(self, blob_uri: str, sas_uri: str) -> None:
        self.blob_reference = _BlobReference(blob_uri, sas_uri)


class _SerializedCredential:
    """A credential model that only exposes `as_dict` with wire (camelCase) names."""

    def __init__(self, blob_uri: str, sas_uri: str) -> None:
        self._payload = {"blobReference": {"blobUri": blob_uri, "credential": {"sasUri": sas_uri, "type": "SAS"}}}

    def as_dict(self) -> dict:
        return json.loads(json.dumps(self._payload))


class _DumpableCredential:
    """A credential model that only exposes pydantic-style `model_dump`."""

    def __init__(self, blob_uri: str, sas_uri: str) -> None:
        self._payload = {"blob_reference": {"blob_uri": blob_uri, "credential": {"sas_uri": sas_uri}}}

    def model_dump(self, mode: str = "python") -> dict:
        del mode
        return json.loads(json.dumps(self._payload))


class _BlobServer:
    """Loopback HTTP server standing in for the SAS-protected blob endpoint."""

    def __init__(self, payload: bytes, *, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        handler = self._handler()
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def _handler(self):
        server = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                self.send_response(server.status)
                self.send_header("Content-Length", str(len(server.payload)))
                self.end_headers()
                if server.status < 400:
                    self.wfile.write(server.payload)

            def log_message(self, *args: object) -> None:
                return

        return _Handler

    def url(self, path: str) -> str:
        host, port = self._httpd.server_address[:2]
        return f"https://{host}:{port}{path}"

    def http_url(self, path: str) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}{path}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _LiveDatasets:
    """Dataset surface shaped like the current SDK: credentials + upload_file, no case index."""

    def __init__(self, inner, *, credential, blob_path: str) -> None:
        self._inner = inner
        self._credential = credential
        self._blob_path = blob_path
        self.get_credentials_calls: list[tuple[str, str]] = []
        self.upload_calls: list[dict] = []
        self.uploaded_rows: dict[str, list[dict]] = {}
        self.observed_temp_paths: list[str] = []
        self.temp_modes: list[int] = []
        self.gets = inner.gets
        self.delete_calls = inner.delete_calls
        self.create_calls = inner.create_calls

    def list(self, **kwargs: object):
        return self._inner.list(**kwargs)

    def list_versions(self, name: str, **kwargs: object):
        return self._inner.list_versions(name, **kwargs)

    def get(self, name: str, version: str, **kwargs: object):
        return self._inner.get(name, version, **kwargs)

    def create_or_update(self, name: str, version: str, dataset_version: object, **kwargs: object):
        return self._inner.create_or_update(name, version, dataset_version, **kwargs)

    def delete(self, name: str, version: str, **kwargs: object):
        return self._inner.delete(name, version, **kwargs)

    def get_credentials(self, name: str, version: str, **kwargs: object):
        del kwargs
        self.get_credentials_calls.append((name, version))
        return self._credential

    def upload_file(self, *, name: str, version: str, file_path: str, connection_name: str | None = None):
        self.upload_calls.append({"name": name, "version": version, "file_path": file_path, "connection_name": connection_name})
        self.observed_temp_paths.append(file_path)
        self.temp_modes.append(os.stat(file_path).st_mode & 0o777)
        rows = [json.loads(line) for line in Path(file_path).read_text(encoding="utf-8").splitlines() if line.strip()]
        self.uploaded_rows[name] = rows
        value = SdkValue(
            {
                "name": name,
                "version": version,
                "id": f"azureai://accounts/example/projects/example/data/{name}/versions/{version}",
                "type": "uri_file",
                "dataUri": f"https://example.blob.core.windows.net/eval/{name}-{version}.jsonl",
                "tags": {},
            }
        )
        self.gets[(name, version)] = value
        return value


def _rows(count: int, *, with_ids: bool = True) -> list[dict]:
    rows = []
    for index in range(count):
        row = {"query": f"question {index}", "response": f"answer {index}"}
        if with_ids:
            row["id"] = f"case-{index:03d}"
        rows.append(row)
    return rows


def _jsonl(rows: list[dict]) -> bytes:
    return ("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n").encode("utf-8")


def _live_adapter(server: _BlobServer, *, credential=None, blob_path: str = "/eval/source.jsonl", scheme: str = "http", reuse: bool = False):
    adapter, fakes = build_fake_adapter(split_writer_available=False, reuse=reuse)
    sas_uri = (server.http_url if scheme == "http" else server.url)(f"{blob_path}?sv=2024-01-01&sig=redacted")
    datasets = _LiveDatasets(
        fakes["datasets"],
        credential=credential if credential is not None else _DatasetCredential(f"https://example.blob.core.windows.net{blob_path}", sas_uri),
        blob_path=blob_path,
    )
    fakes["client"].datasets = datasets
    fakes["live_datasets"] = datasets
    fakes["datasets"].gets[(SOURCE_NAME, SOURCE_VERSION)] = SdkValue(
        {
            "name": SOURCE_NAME,
            "version": SOURCE_VERSION,
            "id": SOURCE_ID,
            "type": "uri_file",
            "dataUri": f"https://example.blob.core.windows.net{blob_path}",
        }
    )
    return adapter, fakes


def _source(**kwargs) -> dict:
    return {"name": SOURCE_NAME, "version": SOURCE_VERSION, "id": SOURCE_ID, **kwargs}


@pytest.fixture()
def server_factory():
    created: list[_BlobServer] = []

    def _make(payload: bytes, *, status: int = 200) -> _BlobServer:
        server = _BlobServer(payload, status=status)
        created.append(server)
        return server

    yield _make
    for item in created:
        item.close()


@pytest.mark.parametrize(
    "credential_factory",
    (_DatasetCredential, _SerializedCredential, _DumpableCredential),
    ids=("model", "as_dict", "model_dump"),
)
def test_every_supported_credential_shape_resolves_the_sas_uri(server_factory, credential_factory) -> None:
    server = server_factory(_jsonl(_rows(3)))
    sas_uri = server.http_url("/eval/source.jsonl?sig=redacted")
    adapter, fakes = _live_adapter(server, credential=credential_factory("https://example.blob.core.windows.net/eval/source.jsonl", sas_uri))

    index = adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    assert [item["row_id"] for item in index] == ["case-000", "case-001", "case-002"]
    assert fakes["live_datasets"].get_credentials_calls == [(SOURCE_NAME, SOURCE_VERSION)]


def test_a_credential_without_a_sas_uri_fails_closed(server_factory) -> None:
    server = server_factory(_jsonl(_rows(2)))

    class _Empty:
        blob_reference = None

    adapter, _fakes = _live_adapter(server, credential=_Empty())

    with pytest.raises(FoundryPrerequisiteError, match="no blob reference"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)


def test_download_is_bounded_by_bytes_and_rows(server_factory, monkeypatch) -> None:
    server = server_factory(_jsonl(_rows(50)))
    adapter, _fakes = _live_adapter(server)
    monkeypatch.setattr(foundry_module, "_MAX_DATASET_BYTES", 128)

    with pytest.raises(FoundryPrerequisiteError, match="exceeds the supported size budget"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    monkeypatch.setattr(foundry_module, "_MAX_DATASET_BYTES", 32 * 1024 * 1024)
    monkeypatch.setattr(foundry_module, "_MAX_DATASET_ROWS", 5)
    adapter, _fakes = _live_adapter(server)

    with pytest.raises(FoundryPrerequisiteError, match="row count exceeds the supported budget"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)


def test_a_rejected_download_is_classified_as_permission(server_factory) -> None:
    server = server_factory(b"", status=403)
    adapter, _fakes = _live_adapter(server)

    with pytest.raises(FoundryPermissionError, match="download was rejected"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)


def test_an_unreachable_blob_is_classified_as_network(server_factory) -> None:
    server = server_factory(_jsonl(_rows(2)))
    adapter, _fakes = _live_adapter(server)
    server.close()

    with pytest.raises(FoundryNetworkError, match="download failed"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)


def test_folder_and_unsupported_layouts_fail_closed(server_factory) -> None:
    server = server_factory(_jsonl(_rows(2)))
    adapter, _fakes = _live_adapter(server, blob_path="/eval/folder/")

    with pytest.raises(FoundryUnsupportedCapabilityError, match="folder or multi-file dataset layouts"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    parquet_server = server_factory(_jsonl(_rows(2)))
    adapter, _fakes = _live_adapter(parquet_server, blob_path="/eval/source.parquet")
    with pytest.raises(FoundryUnsupportedCapabilityError, match="unsupported dataset file type"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)


def test_row_ids_are_stable_and_content_derived_when_absent(server_factory) -> None:
    rows = [
        {"query": "a", "response": "b", "group_id": "g1", "category": "c1"},
        {"query": "c", "response": "d"},
    ]
    server = server_factory(_jsonl(rows))
    adapter, _fakes = _live_adapter(server)

    index = adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    expected = hashlib.sha256(json.dumps(rows[0], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()
    assert index[0]["row_id"] == expected
    assert index[0]["group_id"] == "g1"
    assert index[0]["category"] == "c1"
    assert index[1]["row_id"] != index[0]["row_id"]
    # Identifiers are deterministic across independent reads.
    second, _fakes2 = _live_adapter(server)
    assert [item["row_id"] for item in second.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)] == [item["row_id"] for item in index]


def test_duplicate_stable_identifiers_fail_closed(server_factory) -> None:
    server = server_factory(_jsonl([{"id": "case-1", "query": "a"}, {"id": "case-1", "query": "b"}]))
    adapter, _fakes = _live_adapter(server)

    with pytest.raises(FoundryPrerequisiteError, match="duplicate stable row identifiers"):
        adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)


def test_csv_datasets_are_supported(server_factory) -> None:
    payload = b"id,query,category\ncase-1,hello,greeting\ncase-2,bye,farewell\n"
    server = server_factory(payload)
    adapter, _fakes = _live_adapter(server, blob_path="/eval/source.csv")

    index = adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    assert [item["row_id"] for item in index] == ["case-1", "case-2"]
    assert index[0]["category"] == "greeting"


def test_case_index_never_returns_row_content(server_factory) -> None:
    server = server_factory(_jsonl(_rows(4)))
    adapter, _fakes = _live_adapter(server)

    index = adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    assert all(set(item) <= {"row_id", "group_id", "category"} for item in index)
    assert "question 0" not in json.dumps([dict(item) for item in index])


def _plan(contract, *, operation_id: str = "op-materialize") -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id=operation_id,
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit=RUNTIME_SHA,
        repository_identity="org/repo",
        actions=contract.composite_action(),
    )


def test_the_deterministic_split_is_uploaded_as_two_immutable_versions(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)

    receipt = adapter.apply_resources(_plan(build_contract()))

    live = fakes["live_datasets"]
    assert [call["name"] for call in live.upload_calls] == ["dev-set", "val-set"]
    assert [call["version"] for call in live.upload_calls] == ["1", "1"]
    assert [call["connection_name"] for call in live.upload_calls] == ["foundry-default", "foundry-default"]
    development = live.uploaded_rows["dev-set"]
    validating = live.uploaded_rows["val-set"]
    assert (len(development), len(validating)) == (20, 10)
    development_ids = {row["id"] for row in development}
    validating_ids = {row["id"] for row in validating}
    assert not development_ids & validating_ids
    assert development_ids | validating_ids == {f"case-{index:03d}" for index in range(30)}
    # Uploaded rows are the real content, and the recorded receipt only carries identifiers.
    assert development[0]["query"].startswith("question ")
    assert "question " not in json.dumps(receipt.model_dump(mode="json"))


def test_temporary_split_files_are_removed_immediately(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)

    adapter.apply_resources(_plan(build_contract()))

    live = fakes["live_datasets"]
    assert live.observed_temp_paths
    assert all(not Path(path).exists() for path in live.observed_temp_paths)
    if os.name != "nt":
        assert all(mode == 0o600 for mode in live.temp_modes)


def test_no_raw_row_content_is_persisted_anywhere(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)

    receipt = adapter.apply_resources(_plan(build_contract()))
    state = adapter.export_provider_state(receipt)
    checkpoint = adapter.onboarding_ledger_snapshot()

    for document in (state, checkpoint, receipt.model_dump(mode="json")):
        serialized = json.dumps(document, sort_keys=True, default=str)
        assert "question " not in serialized
        assert "answer " not in serialized
    # The in-memory row cache is dropped as soon as the run finishes.
    assert adapter._dataset_row_cache == {}


def test_an_already_uploaded_split_is_adopted_instead_of_re_uploaded(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)
    contract = build_contract()
    source = _source()
    index = adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)
    case_ids = [str(item["row_id"]) for item in index][:20]

    first = adapter.publish_split_dataset(
        source_dataset=source,
        role="development",
        case_ids=case_ids,
        dataset_name="dev-set",
        dataset_version="1",
        dataset_type="uri_file",
        connection_name="foundry-default",
        operation_id="op-1",
        action_id="evaluations:app:onboarding:dataset:development",
    )
    second = adapter.publish_split_dataset(
        source_dataset=source,
        role="development",
        case_ids=case_ids,
        dataset_name="dev-set",
        dataset_version="1",
        dataset_type="uri_file",
        connection_name="foundry-default",
        operation_id="op-1",
        action_id="evaluations:app:onboarding:dataset:development",
    )

    assert first["created"] is True and second["adopted"] is True
    assert len(fakes["live_datasets"].upload_calls) == 1
    assert first["resource_id"] == second["resource_id"]
    del contract


def test_a_restarted_run_adopts_the_checkpointed_upload(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    first_adapter, fakes = _live_adapter(server)
    contract = build_contract()
    source = _source()
    index = first_adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)
    case_ids = [str(item["row_id"]) for item in index][:20]
    pending: list[dict] = []
    first_adapter.publish_split_dataset(
        source_dataset=source,
        role="development",
        case_ids=case_ids,
        dataset_name="dev-set",
        dataset_version="1",
        dataset_type="uri_file",
        connection_name="foundry-default",
        operation_id="op-1",
        action_id="evaluations:app:onboarding:dataset:development",
        on_pending=pending.append,
    )
    assert pending and pending[0]["dataset_name"] == "dev-set"

    # A fresh process restores only the durable checkpoint, then republishes the same split.
    restarted, _ = _live_adapter(server)
    restarted._client.datasets = fakes["live_datasets"]
    restarted.restore_checkpoint(
        {
            "schema_version": 1,
            "onboarding": {
                "evaluations:app:onboarding": {
                    "stages": {"split": {"status": "in_flight", "pending_splits": {"development": pending[0]}}},
                    "finalization": None,
                }
            },
        }
    )
    resumed = restarted.publish_split_dataset(
        source_dataset=source,
        role="development",
        case_ids=case_ids,
        dataset_name="dev-set",
        dataset_version="1",
        dataset_type="uri_file",
        connection_name="foundry-default",
        operation_id="op-1",
        action_id="evaluations:app:onboarding:dataset:development",
    )

    assert resumed["adopted"] is True and resumed["replayed"] is True
    assert len(fakes["live_datasets"].upload_calls) == 1
    del contract


def test_a_foreign_dataset_version_with_the_same_name_fails_closed(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)
    fakes["datasets"].gets[("dev-set", "1")] = SdkValue(
        {
            "name": "dev-set",
            "version": "1",
            "id": "azureai://accounts/example/projects/example/data/dev-set/versions/1",
            "type": "uri_file",
            "dataUri": "https://example.blob.core.windows.net/eval/someone-elses.jsonl",
        }
    )
    index = adapter.dataset_case_index(SOURCE_NAME, SOURCE_VERSION)

    with pytest.raises(FoundryPrerequisiteError, match="already exists with different content"):
        adapter.publish_split_dataset(
            source_dataset=_source(),
            role="development",
            case_ids=[str(item["row_id"]) for item in index][:20],
            dataset_name="dev-set",
            dataset_version="1",
            dataset_type="uri_file",
            connection_name="foundry-default",
            operation_id="op-1",
            action_id="evaluations:app:onboarding:dataset:development",
        )

    assert fakes["live_datasets"].upload_calls == []


def test_a_split_referencing_an_unknown_case_fails_closed(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)

    with pytest.raises(FoundryPrerequisiteError, match="not present in the source dataset"):
        adapter.publish_split_dataset(
            source_dataset=_source(),
            role="development",
            case_ids=["case-999"],
            dataset_name="dev-set",
            dataset_version="1",
            dataset_type="uri_file",
            connection_name="foundry-default",
            operation_id="op-1",
            action_id="evaluations:app:onboarding:dataset:development",
        )

    assert fakes["live_datasets"].upload_calls == []


def test_created_only_rollback_removes_uploaded_splits(server_factory) -> None:
    server = server_factory(_jsonl(_rows(30)))
    adapter, fakes = _live_adapter(server)
    receipt = adapter.apply_resources(_plan(build_contract(), operation_id="op-rollback"))

    adapter.rollback_resources(receipt)

    assert sorted(fakes["datasets"].delete_calls) == [("dev-set", "1"), ("val-set", "1")]
    assert adapter.verify_rollback(receipt) is True
