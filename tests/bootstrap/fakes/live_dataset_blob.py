"""A reusable, API-faithful loopback stand-in for the Foundry dataset SAS surface.

`FoundryAdapter`'s default live path for dataset materialization is: `datasets.get_credentials`
-> a bounded HTTP download from the returned SAS URI -> deterministic split ->
`datasets.upload_file`. Several tests exercise that exact path against a real loopback HTTP
server rather than short-circuiting it with the `get_case_index`/`split_writer` preview seams,
so callers can assert the driver/provider genuinely downloaded and re-uploaded content instead
of relying on injected pre-existing resources. This module factors the shared plumbing out so
more than one test can reuse it without duplicating the loopback server and credential shapes.
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path

from tests.bootstrap.fakes.foundry_env import SdkValue


class DatasetBlobServer:
    """Loopback HTTP server standing in for a SAS-protected blob endpoint."""

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
        return f"http://{host}:{port}{path}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class LiveDatasets:
    """Dataset surface shaped like the current SDK: `get_credentials` + `upload_file`.

    Deliberately does *not* implement `get_case_index`, so `FoundryAdapter.dataset_case_index`
    always falls through to its default live path (a real bounded HTTP download through the
    SAS credential returned here), and every split it publishes is genuinely uploaded through
    `upload_file` rather than short-circuited by an injected `split_writer`.
    """

    def __init__(self, inner, *, credential, blob_path: str) -> None:
        self._inner = inner
        self._credential = credential
        self._blob_path = blob_path
        self.get_credentials_calls: list[tuple[str, str]] = []
        self.upload_calls: list[dict[str, object]] = []
        self.uploaded_rows: dict[str, list[dict[str, object]]] = {}
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


class DatasetCredential:
    """Shaped like `DatasetCredential`."""

    def __init__(self, blob_uri: str, sas_uri: str) -> None:
        self.blob_reference = _BlobReference(blob_uri, sas_uri)


def synthetic_rows(count: int) -> list[dict[str, object]]:
    """Real (if synthetic) generated-dataset row content, with explicit stable ids."""

    return [{"id": f"case-{index:03d}", "query": f"question {index}", "response": f"answer {index}"} for index in range(count)]


def rows_to_jsonl(rows: list[dict[str, object]]) -> bytes:
    return ("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n").encode("utf-8")


def install_live_datasets(
    adapter,
    fakes: dict[str, object],
    *,
    dataset_name: str,
    dataset_version: str = "1",
    rows: list[dict[str, object]],
    blob_path: str = "/eval/generated.jsonl",
) -> tuple[DatasetBlobServer, LiveDatasets]:
    """Replace a fake adapter's dataset surface with the real materialization path.

    Starts a loopback HTTP server serving `rows` as JSONL, registers a real SAS-shaped
    credential for `(dataset_name, dataset_version)`, and swaps the adapter's `datasets`
    client for one that only exposes `get_credentials`/`upload_file` -- exactly the seam
    `FoundryAdapter`'s default live path uses, with no `get_case_index`/`split_writer`
    short-circuit available. The adapter itself must already have been built with
    `split_writer_available=False` so `publish_split_dataset` also takes its live path.
    """

    server = DatasetBlobServer(rows_to_jsonl(rows))
    sas_uri = server.url(f"{blob_path}?sv=2024-01-01&sig=redacted")
    credential = DatasetCredential(f"https://example.blob.core.windows.net{blob_path}", sas_uri)
    live_datasets = LiveDatasets(fakes["datasets"], credential=credential, blob_path=blob_path)
    fakes["client"].datasets = live_datasets
    fakes["live_datasets"] = live_datasets
    # `dataset_case_index`/`_dataset_blob_credential` never consult this entry directly (they
    # always resolve content through `get_credentials`), but keeping the registry consistent
    # lets any code path that lists/gets the dataset by name/version still find it.
    live_datasets.gets[(dataset_name, dataset_version)] = SdkValue(
        {
            "name": dataset_name,
            "version": dataset_version,
            "id": f"azureai://accounts/example/projects/example/data/{dataset_name}/versions/{dataset_version}",
            "type": "uri_file",
            "dataUri": f"https://example.blob.core.windows.net{blob_path}",
        }
    )
    adapter._client.datasets = live_datasets
    return server, live_datasets


__all__ = [
    "DatasetBlobServer",
    "DatasetCredential",
    "LiveDatasets",
    "install_live_datasets",
    "rows_to_jsonl",
    "synthetic_rows",
]
