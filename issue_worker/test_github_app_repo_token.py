"""Repository-scoped installation tokens for one hosted job container."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import github_app_auth as auth_module

PEM_MARKER = "PRIVATE KEY"


class RepositoryTokenTests(unittest.TestCase):
    def _config(self, root: Path) -> Path:
        key = root / "bot.pem"
        key.write_text("not used\n", encoding="utf-8")
        key.chmod(0o600)
        config = root / "apps.json"
        config.write_text(
            json.dumps(
                {
                    "claude": {
                        "app_id": 418,
                        "installation_id": 100,
                        "private_key_path": str(key),
                        "bot_login": "swarm-claude-bot[bot]",
                        "installations": {"acme": 555},
                    }
                }
            ),
            encoding="utf-8",
        )
        return config

    def test_token_is_one_repository_and_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-repo-token.") as temporary:
            config = self._config(Path(temporary))
            seen: list[dict[str, object]] = []

            def urlopen(request, timeout=30):
                del timeout
                seen.append(json.loads(request.data.decode("utf-8")))
                body = json.dumps({"token": f"ghs_tok{len(seen)}value123456"}).encode()
                return io.BytesIO(body)

            with mock.patch.object(auth_module.GitHubAppAuth, "_jwt", return_value="jwt"):
                with mock.patch.object(auth_module.urllib.request, "urlopen", side_effect=urlopen) as opened:
                    auth = auth_module.GitHubAppAuth(config, repository="acme/demo")
                    first = auth.repository_scoped_token("claude", "acme/demo")
                    second = auth.repository_scoped_token("claude", "acme/demo")
            self.assertEqual(opened.call_count, 2)
            self.assertNotEqual(first, second)
            self.assertEqual(len(seen), 2)
            for body in seen:
                self.assertEqual(body["repositories"], ["demo"])
                self.assertEqual(body["permissions"], dict(auth_module.REPOSITORY_TOKEN_PERMISSIONS))
            self.assertIn("installations/555/access_tokens", opened.call_args_list[0].args[0].full_url)
            self.assertNotIn("ghs_tok", config.read_text(encoding="utf-8"))

    def test_a_repository_that_is_not_one_owner_and_name_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-repo-token-bad.") as temporary:
            config = self._config(Path(temporary))
            auth = auth_module.GitHubAppAuth(config, repository="acme/demo")
            for repository in ("owner/name/extra", " owner/name", "nameonly", "owner/ name"):
                with self.assertRaises(RuntimeError):
                    auth.repository_scoped_token("claude", repository)

    def test_mint_document_and_cli_keep_the_key_off_the_result(self) -> None:
        with tempfile.TemporaryDirectory(prefix="swarm-mint-doc.") as temporary:
            root = Path(temporary)
            key = root / "real.pem"
            subprocess.run(
                ["openssl", "genrsa", "-out", str(key), "2048"],
                check=True,
                capture_output=True,
            )
            pem = key.read_text(encoding="utf-8")
            self.assertIn(PEM_MARKER, pem)
            document = {
                "app_id": 418,
                "installation_id": 555,
                "private_key_pem": pem,
                "repository": "acme/demo",
                "provider": "claude",
            }
            created: list[Path] = []
            real = tempfile.TemporaryDirectory

            class Spy:
                def __init__(self, *args, **kwargs):
                    self._inner = real(*args, **kwargs)
                    created.append(Path(self._inner.name))

                def __enter__(self):
                    return self._inner.__enter__()

                def __exit__(self, exc_type, exc, tb):
                    return self._inner.__exit__(exc_type, exc, tb)

            def urlopen(request, timeout=30):
                del request, timeout
                return io.BytesIO(json.dumps({"token": "ghs_mintedtokenvalue123"}).encode())

            with mock.patch.object(auth_module.tempfile, "TemporaryDirectory", Spy):
                with mock.patch.object(auth_module.urllib.request, "urlopen", side_effect=urlopen):
                    minted = auth_module.mint_repository_token_document(document)
            self.assertEqual(minted["token"], "ghs_mintedtokenvalue123")
            self.assertNotIn(PEM_MARKER, minted["token"])
            self.assertTrue(created)
            self.assertFalse(created[0].exists())

            stdout = io.StringIO()
            argv = ["mint-repository-token"]
            with mock.patch.object(auth_module.urllib.request, "urlopen", side_effect=urlopen):
                with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(document))):
                    with mock.patch.object(sys, "stdout", stdout):
                        code = auth_module.main(argv)
            self.assertEqual(code, 0)
            self.assertEqual(argv, ["mint-repository-token"])
            self.assertNotIn(pem, " ".join(argv))
            printed = json.loads(stdout.getvalue())
            self.assertEqual(set(printed), {"token"})
            self.assertNotIn(PEM_MARKER, stdout.getvalue())
            self.assertNotIn(pem, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
