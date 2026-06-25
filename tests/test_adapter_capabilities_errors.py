import unittest

from agentdeck.adapters.capabilities import adapter_capabilities_for_name, adapter_requires_provider_session
from agentdeck.adapters.codex_exec import CodexExecAdapter
from agentdeck.adapters.echo import EchoAdapter
from agentdeck.adapters.errors import (
    AdapterErrorKind,
    classify_adapter_error,
    command_not_found_payload,
    working_directory_not_found_payload,
)
from agentdeck.adapters.kimi_print import KimiPrintAdapter


class AdapterCapabilitiesAndErrorsTests(unittest.TestCase):
    def test_capabilities_are_declared_for_known_adapters(self) -> None:
        self.assertTrue(CodexExecAdapter.capabilities.supports_resume)
        self.assertTrue(CodexExecAdapter.capabilities.supports_approval_bypass)
        self.assertTrue(KimiPrintAdapter.capabilities.supports_resume_last)
        self.assertTrue(KimiPrintAdapter.capabilities.emits_provider_session_id)
        self.assertFalse(KimiPrintAdapter.capabilities.supports_approval_record)
        self.assertFalse(EchoAdapter.capabilities.requires_provider_session)

    def test_capability_lookup_handles_aliases(self) -> None:
        self.assertTrue(adapter_requires_provider_session("codex"))
        self.assertTrue(adapter_requires_provider_session("codex_exec"))
        self.assertTrue(adapter_requires_provider_session("kimi-print"))
        self.assertFalse(adapter_requires_provider_session("echo"))
        self.assertFalse(adapter_capabilities_for_name("unknown").supports_resume)

    def test_error_classifier_returns_stable_kinds(self) -> None:
        self.assertEqual(
            classify_adapter_error("401 unauthorized")["error_kind"],
            AdapterErrorKind.AUTH_FAILED.value,
        )
        self.assertEqual(
            classify_adapter_error("429 rate limit exceeded")["error_kind"],
            AdapterErrorKind.RATE_LIMIT.value,
        )
        self.assertEqual(
            classify_adapter_error("Selected model is at capacity. Please try a different model.")["error_kind"],
            AdapterErrorKind.RATE_LIMIT.value,
        )
        self.assertEqual(
            classify_adapter_error("Heads up, you have less than 25% of your weekly limit left.")["error_kind"],
            AdapterErrorKind.USAGE_WARNING.value,
        )
        self.assertEqual(
            classify_adapter_error("model not found")["error_kind"],
            AdapterErrorKind.MODEL_NOT_FOUND.value,
        )
        self.assertEqual(
            classify_adapter_error("invalid session for resume")["error_kind"],
            AdapterErrorKind.INVALID_RESUME_SESSION.value,
        )
        self.assertEqual(
            classify_adapter_error("approval requested")["error_kind"],
            AdapterErrorKind.APPROVAL_REQUIRED.value,
        )
        self.assertEqual(
            classify_adapter_error("process failed", return_code=2)["error_kind"],
            AdapterErrorKind.PROCESS_FAILED.value,
        )

    def test_command_not_found_payload_is_actionable(self) -> None:
        payload = command_not_found_payload("codex")

        self.assertEqual(payload["error_kind"], AdapterErrorKind.COMMAND_NOT_FOUND.value)
        self.assertEqual(payload["binary"], "codex")
        self.assertIn("Install", payload["hint"])

    def test_working_directory_not_found_payload_is_actionable(self) -> None:
        payload = working_directory_not_found_payload("/tmp/missing-project")

        self.assertEqual(payload["error_kind"], AdapterErrorKind.WORKING_DIRECTORY_NOT_FOUND.value)
        self.assertEqual(payload["cwd"], "/tmp/missing-project")
        self.assertIn("Create", payload["hint"])


if __name__ == "__main__":
    unittest.main()
