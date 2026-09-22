#!/usr/bin/env python3

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import issue_images
from issue_images import (
    ImageDownloadError,
    IssueImage,
    assistant_result_text,
    claude_stream_message,
    codex_image_flags,
    download_issue_image,
    extract_issue_image_refs,
    fetch_image_bytes,
    format_image_note,
    grok_prompt_json,
    inlined_images,
    is_issue_image_url,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
ASSET = "https://github.com/acme/widgets/assets/42/11111111-2222-3333-4444-555555555555"
ATTACHMENT = "https://github.com/user-attachments/assets/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
USER_IMAGE = "https://user-images.githubusercontent.com/1/22233344-5555-6666-7777-888888888888.png"


class _Response:
    def __init__(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.status = status
        self.headers = headers
        self._body = body

    def read(self, _limit: int) -> bytes:
        return self._body

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Opener:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.requests: list[object] = []

    def open(self, request: object, timeout: float | None = None) -> _Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request")
        return self.responses.pop(0)


class IssueImageTests(unittest.TestCase):
    def test_extract_keeps_github_images_in_order_and_drops_other_links(self) -> None:
        body = "\n".join(
            [
                f"See ![broken layout]({ATTACHMENT}).",
                "Also https://imgur.com/not-this.png and [docs](https://github.com/acme/widgets).",
                f'<img alt="mobile" src="{USER_IMAGE}">',
                f"Repeated {ATTACHMENT}",
                "Diagram: https://github.com/acme/widgets/raw/main/docs/flow.svg",
                f"Legacy upload {ASSET}.",
            ]
        )
        refs = extract_issue_image_refs(body)
        self.assertEqual([url for url, _alt in refs], [ATTACHMENT, USER_IMAGE, ASSET])
        self.assertEqual(refs[0][1], "broken layout")
        self.assertEqual(refs[1][1], "mobile")
        self.assertFalse(is_issue_image_url("https://127.0.0.1/secret.png"))
        self.assertFalse(is_issue_image_url("http://github.com/user-attachments/assets/abc"))

    def test_extract_unescapes_html_and_accepts_the_configured_github_host(self) -> None:
        url = "https://github.example.com/user-attachments/assets/abc-def?size=2&amp;v=1"
        refs = extract_issue_image_refs(
            f'<img src="{url}" alt="shot">',
            github_host="github.example.com",
        )
        self.assertEqual(
            refs,
            [("https://github.example.com/user-attachments/assets/abc-def?size=2&v=1", "shot")],
        )
        self.assertEqual(extract_issue_image_refs(f'<img src="{url}">'), [])

    def test_download_drops_the_token_when_github_redirects_to_its_cdn(self) -> None:
        opener = _Opener(
            [
                _Response(302, {"Location": "https://private-user-images.githubusercontent.com/pic.png"}),
                _Response(200, {"Content-Type": "image/png"}, PNG),
            ]
        )
        data, content_type = fetch_image_bytes(
            ATTACHMENT,
            token="secret",
            timeout=5,
            opener=opener,  # type: ignore[arg-type]
        )
        self.assertEqual(data, PNG)
        self.assertIn("image/png", content_type)
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer secret")  # type: ignore[attr-defined]
        self.assertIsNone(opener.requests[1].get_header("Authorization"))  # type: ignore[attr-defined]

    def test_download_refuses_a_redirect_to_a_link_local_address(self) -> None:
        opener = _Opener([_Response(302, {"Location": "http://169.254.169.254/latest/meta-data"})])
        with self.assertRaises(ImageDownloadError):
            fetch_image_bytes(ATTACHMENT, token="secret", timeout=5, opener=opener)  # type: ignore[arg-type]
        self.assertEqual(len(opener.requests), 1)

    def test_download_rejects_an_html_sign_in_page(self) -> None:
        def fetch(_url: str, _token: str = "", *, timeout: float = 30) -> tuple[bytes, str]:
            return b"<html>sign in</html>", "text/html"

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ImageDownloadError):
                download_issue_image(
                    ATTACHMENT,
                    "sign-in",
                    "issue description",
                    Path(directory),
                    fetch=fetch,
                )

    def test_saved_image_note_names_each_file_in_order(self) -> None:
        self.assertEqual(format_image_note([]), "")
        image = IssueImage(ATTACHMENT + "?token=secret", Path("/tmp/shot.png"), "image/png", "broken layout", "issue description")
        note = format_image_note([image])
        self.assertIn("Image 1", note)
        self.assertIn("/tmp/shot.png", note)
        self.assertIn("broken layout", note)
        self.assertIn("issue description", note)
        self.assertNotIn("token=secret", note)

    def test_provider_payloads_put_the_image_before_the_text(self) -> None:
        path = self._png_file()
        claude = json.loads(claude_stream_message("Fix the layout", [(PNG, "image/png")]))
        content = claude["message"]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"]["media_type"], "image/png")
        self.assertEqual(content[1], {"type": "text", "text": "Fix the layout"})
        grok = json.loads(grok_prompt_json("Fix the layout", [(PNG, "image/png")]))
        self.assertEqual(grok[0]["type"], "image")
        self.assertEqual(grok[0]["mimeType"], "image/png")
        self.assertEqual(grok[1]["text"], "Fix the layout")
        self.assertEqual(codex_image_flags([path]), ["--image", str(path)])

    def test_inline_budget_drops_images_that_do_not_fit(self) -> None:
        path = self._png_file()
        original = issue_images.GROK_PROMPT_JSON_BUDGET
        issue_images.GROK_PROMPT_JSON_BUDGET = 40
        try:
            self.assertEqual(inlined_images("describe this screenshot", [path], kind="grok"), [])
        finally:
            issue_images.GROK_PROMPT_JSON_BUDGET = original
        loaded = inlined_images("describe this screenshot", [path], kind="grok")
        self.assertEqual(loaded, [(PNG, "image/png")])

    def test_assistant_result_text_reads_stream_json_and_a_single_result_object(self) -> None:
        stream = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init"}),
                json.dumps({"type": "result", "result": "done\n"}),
            ]
        )
        self.assertEqual(assistant_result_text(stream), "done\n")
        self.assertEqual(assistant_result_text(json.dumps({"type": "result", "result": "grade"})), "grade")
        self.assertEqual(assistant_result_text(json.dumps({"text": "plain"})), "")

    def _png_file(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="swarm-image-test."))
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        path = directory / "shot.png"
        path.write_bytes(PNG)
        return path


if __name__ == "__main__":
    unittest.main()
