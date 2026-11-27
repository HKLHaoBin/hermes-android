#!/usr/bin/env python3
"""Static contract checks for Issue #14 integration preparation.

These tests inspect workflow/documentation seams only. They do not build an APK,
run Gradle, connect to ADB, or create a hardware receipt.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import android_device_matrix as matrix
import create_android_artifact_manifest as artifact_builder


BUILD_WORKFLOW = ROOT / ".github/workflows/build-apk.yml"
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish-apk.yml"
HARNESS = ROOT / "scripts/android_device_matrix.py"
MANIFEST_BUILDER = ROOT / "scripts/create_android_artifact_manifest.py"
ACCEPTANCE = ROOT / "docs/android-device-acceptance.md"
README = ROOT / "README.md"


class AndroidIntegrationPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = BUILD_WORKFLOW.read_text(encoding="utf-8")
        cls.publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.harness = HARNESS.read_text(encoding="utf-8")
        cls.builder = MANIFEST_BUILDER.read_text(encoding="utf-8")
        cls.acceptance = ACCEPTANCE.read_text(encoding="utf-8")
        cls.readme = README.read_text(encoding="utf-8")

    def test_static_issue14_contract_runs_before_android_build(self):
        self.assertIn("python3 scripts/android_device_matrix.py validate-matrix docs/android-device-matrix.json", self.build)
        self.assertIn("python3 scripts/test_android_device_matrix.py", self.build)
        self.assertIn("python3 scripts/test_android_device_integration.py", self.build)
        static_step = self.build.index("Issue #14 device matrix static validation")
        gradle_step = self.build.index("gradle assembleRelease")
        self.assertLess(static_step, gradle_step)
        self.assertNotRegex(self.build, r"android_device_matrix\.py\s+(?:capture|record|gate)")

    def test_build_creates_and_validates_exact_actions_artifact_identity(self):
        for required in (
            "Create Actions artifact identity receipt",
            "scripts/create_android_artifact_manifest.py",
            "dist/artifact-manifest.json",
            "build/deb/meta.json",
            "build/deb/root/data/data/com.termux/files/usr/lib/hermes-agent/app/install-stamp.json",
            "build/payload/payload.json",
            "build/webui/ANDROID_WEBUI_REVISION",
            "android/release-cert.sha256",
            "APP_VERSION_NAME: '0.1.2'",
            "APP_VERSION_CODE: ${{ github.run_number }}",
            "HERMES_ANDROID_VERSION_NAME: ${{ env.APP_VERSION_NAME }}",
            "HERMES_ANDROID_VERSION_CODE: ${{ env.APP_VERSION_CODE }}",
            '--version-name "$APP_VERSION_NAME"',
            '--version-code "$APP_VERSION_CODE"',
            "--repository \"$GITHUB_REPOSITORY\"",
            "--workflow \"$GITHUB_WORKFLOW\"",
            "--artifact-name apk",
            "--run-id \"$GITHUB_RUN_ID\"",
            "--head-sha \"$GITHUB_SHA\"",
            "validate-artifact",
            "name: apk",
            "path: dist/",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.build)
        self.assertLess(self.build.index("Verify APK signature"), self.build.index("Create Actions artifact identity receipt"))
        self.assertLess(self.build.index("Create Actions artifact identity receipt"), self.build.index("actions/upload-artifact@v5"))

    def test_manifest_builder_reconciles_runtime_payload_and_webui_identity(self):
        for required in (
            "build_artifact_manifest",
            "runtime_meta",
            "install_stamp",
            "payload_manifest",
            "payload_id",
            "upstream runtime SHA-256 differs from runtime meta",
            "upstream source commit differs from install stamp",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.builder)
        self.assertIn("ANDROID_WEBUI_REVISION", self.build)
        for required in ("artifact_name", "run_id", "head_sha", "payload_id", "webui_revision"):
            with self.subTest(required=required):
                self.assertIn(required, self.harness)

    def test_manifest_builder_reads_exact_sources_and_rejects_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / "candidate.apk"
            apk.write_bytes(b"synthetic artifact bytes")
            runtime_meta = root / "meta.json"
            runtime_meta.write_text(json.dumps({"version": "runtime-test", "sha256": "d" * 64}), encoding="utf-8")
            install_stamp = root / "install-stamp.json"
            install_stamp.write_text(json.dumps({"commit": "e" * 40}), encoding="utf-8")
            payload_manifest = root / "payload.json"
            payload_manifest.write_text(json.dumps({
                "payload_id": "payload-test",
                "upstream": {"deb": {"sha256": "d" * 64}, "stamp": {"commit": "e" * 40}},
            }), encoding="utf-8")
            webui_revision = root / "ANDROID_WEBUI_REVISION"
            webui_revision.write_text("f" * 40, encoding="utf-8")
            signer = root / "release-cert.sha256"
            signer.write_text("b" * 64, encoding="utf-8")
            args = argparse.Namespace(
                apk=apk,
                output=root / "artifact-manifest.json",
                runtime_meta=runtime_meta,
                install_stamp=install_stamp,
                payload_manifest=payload_manifest,
                webui_revision_file=webui_revision,
                signer_file=signer,
                application_id="io.github.hkhaobin.hermeswebui",
                version_name="0.1.2",
                version_code=99,
                repository="ExampleOwner/example-repo",
                workflow="Build APK",
                artifact_name="apk",
                run_id="12345",
                head_sha="c" * 40,
            )
            manifest = artifact_builder.create_manifest(args)
            self.assertEqual(matrix.validate_artifact(manifest), [])
            self.assertEqual(manifest["runtime"]["payload_id"], "payload-test")
            payload_manifest.write_text(json.dumps({
                "payload_id": "payload-test",
                "upstream": {"deb": {"sha256": "0" * 64}, "stamp": {"commit": "e" * 40}},
            }), encoding="utf-8")
            with self.assertRaisesRegex(matrix.HarnessError, "runtime SHA-256"):
                artifact_builder.create_manifest(args)

    def test_publish_requires_the_same_actions_identity_and_releases_manifest(self):
        for required in (
            'run=$(gh api "repos/$GH_REPO/actions/runs/$RUN_ID")',
            'gh run download "$RUN_ID" --name apk --dir dist',
            "artifact-manifest.json",
            'build.get("repository") == repository',
            'build.get("artifact_name") == "apk"',
            'build.get("run_id") == run_id',
            'build.get("head_sha") == head_sha',
            "manifest_apk",
            "manifest_digest",
            "artifact-manifest.json --clobber",
            'and .state=="uploaded"'):
            with self.subTest(required=required):
                self.assertIn(required, self.publish)

    def test_staging_pr_seams_are_explicit_and_do_not_claim_hardware_results(self):
        for required in (
            "integration/issue-1-all-prs",
            "PR #2",
            "PR #3",
            "PR #4",
            "__android/status.json",
            "RD-JOB-01",
            "RD-JOB-02",
            "BLOCKED",
            "NOT RUN",
            "no real-device result",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.acceptance)
        self.assertIn("artifact-manifest.json", self.readme)
        self.assertIn("Actions artifact", self.readme)

    def test_hardware_execution_remains_outside_build_and_publish_workflows(self):
        self.assertNotRegex(self.build, r"\b(?:adb|capture|record|gate)\b")
        self.assertNotRegex(self.publish, r"\b(?:adb|capture|record|gate)\b")
        self.assertRegex(self.acceptance, r"no authorized online device.*code `2`.*writes no receipt")


if __name__ == "__main__":
    unittest.main()
