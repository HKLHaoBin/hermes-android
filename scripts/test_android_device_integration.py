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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import android_device_matrix as matrix
import create_android_artifact_manifest as artifact_builder
import verify_android_artifact_manifest as artifact_verifier


BUILD_WORKFLOW = ROOT / ".github/workflows/build-apk.yml"
PUBLISH_WORKFLOW = ROOT / ".github/workflows/publish-apk.yml"
HARNESS = ROOT / "scripts/android_device_matrix.py"
MANIFEST_BUILDER = ROOT / "scripts/create_android_artifact_manifest.py"
MANIFEST_VERIFIER = ROOT / "scripts/verify_android_artifact_manifest.py"
JOB_LEDGER_SOURCE = ROOT / "android/app/src/main/java/io/github/hermesandroid/JobLedger.kt"
JOB_LEDGER_DOC = ROOT / "docs/job-ledger.md"
JOB_LEDGER_CONTRACT = ROOT / "scripts/test_job_ledger_contract.py"
JOB_LEDGER_SOURCE_TEST = ROOT / "scripts/test_job_ledger_source.py"
ACCEPTANCE = ROOT / "docs/android-device-acceptance.md"
README = ROOT / "README.md"


class AndroidIntegrationPreparationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = BUILD_WORKFLOW.read_text(encoding="utf-8")
        cls.publish = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
        cls.harness = HARNESS.read_text(encoding="utf-8")
        cls.builder = MANIFEST_BUILDER.read_text(encoding="utf-8")
        cls.verifier = MANIFEST_VERIFIER.read_text(encoding="utf-8")
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

    def test_current_main_job_ledger_contract_is_preserved_in_the_pr_tree(self):
        for path in (JOB_LEDGER_SOURCE, JOB_LEDGER_DOC, JOB_LEDGER_CONTRACT, JOB_LEDGER_SOURCE_TEST):
            with self.subTest(path=path):
                self.assertTrue(path.is_file())
        for required in (
            "Job ledger contract regressions",
            "python3 scripts/test_job_ledger_contract.py",
            "Job ledger source contract regressions",
            "python3 scripts/test_job_ledger_source.py",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.build)

    def test_build_creates_and_validates_exact_actions_artifact_identity(self):
        for required in (
            "Create Actions artifact identity receipt",
            "scripts/create_android_artifact_manifest.py",
            "dist/artifact-manifest.json",
            "build/deb/meta.json",
            "build/deb/root/data/data/com.termux/files/usr/lib/hermes-agent/app/install-stamp.json",
            "build/payload/payload.json",
            "build/webui/ANDROID_WEBUI_REVISION",
            "cp build/deb/meta.json dist/runtime-meta.json",
            "cp build/deb/root/data/data/com.termux/files/usr/lib/hermes-agent/app/install-stamp.json dist/install-stamp.json",
            "cp build/webui/ANDROID_WEBUI_REVISION dist/webui-revision.txt",
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

    def test_publish_verifier_rebinds_apk_runtime_payload_and_webui_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / "hermes-android-test.apk"
            apk.write_bytes(b"synthetic APK bytes")
            runtime_meta = root / "runtime-meta.json"
            runtime_meta.write_text(json.dumps({"version": "runtime-test", "sha256": "d" * 64}), encoding="utf-8")
            install_stamp = root / "install-stamp.json"
            install_stamp.write_text(json.dumps({"commit": "e" * 40}), encoding="utf-8")
            webui = root / "webui-revision.txt"
            webui.write_text("f" * 40, encoding="utf-8")
            cert = root / "release-cert.sha256"
            cert.write_text("b" * 64, encoding="utf-8")
            payload_zip = root / "payload.zip"
            payload_zip.write_bytes(b"synthetic payload zip")
            payload_id = f"runtime-test-{'d' * 12}-webui-{matrix.sha256_file(payload_zip)[:16]}"
            payload = {
                "payload_id": payload_id,
                "zip_bytes": payload_zip.stat().st_size,
                "upstream": {
                    "deb": {"version": "runtime-test", "sha256": "d" * 64},
                    "stamp": {"commit": "e" * 40},
                },
            }
            payload_manifest = root / "payload.json"
            payload_manifest.write_text(json.dumps(payload), encoding="utf-8")
            payload_artifact_manifest = root / "payload-artifact.json"
            payload_artifact_manifest.write_bytes(payload_manifest.read_bytes())
            artifact = matrix.build_artifact_manifest(
                apk,
                application_id="io.github.hkhaobin.hermeswebui",
                version_name="0.1.2",
                version_code=99,
                signer_sha256="b" * 64,
                build_run_id="12345",
                head_sha="c" * 40,
                runtime_version="runtime-test",
                runtime_sha256="d" * 64,
                runtime_source_commit="e" * 40,
                payload_id=payload_id,
                webui_revision="f" * 40,
                repository=matrix.EXPECTED_REPOSITORY,
            )
            manifest = root / "artifact-manifest.json"
            manifest.write_text(json.dumps(artifact), encoding="utf-8")
            with patch.object(
                artifact_verifier,
                "read_apk_metadata",
                return_value={"application_id": "io.github.hkhaobin.hermeswebui", "version_name": "0.1.2", "version_code": 99},
            ):
                verified = artifact_verifier.verify_manifest(
                    manifest_path=manifest,
                    apk_path=apk,
                    payload_manifest_path=payload_manifest,
                    payload_artifact_manifest_path=payload_artifact_manifest,
                    payload_zip_path=payload_zip,
                    runtime_meta_path=runtime_meta,
                    install_stamp_path=install_stamp,
                    webui_revision_path=webui,
                    release_cert_path=cert,
                    aapt_path=root / "aapt",
                    signer_sha256="b" * 64,
                    repository=matrix.EXPECTED_REPOSITORY,
                    workflow="Build APK",
                    artifact_name="apk",
                    run_id="12345",
                    head_sha="c" * 40,
                )
                self.assertEqual(verified["runtime"]["payload_id"], payload_id)
                artifact["apk"]["version_code"] = 100
                manifest.write_text(json.dumps(artifact), encoding="utf-8")
                with self.assertRaisesRegex(matrix.HarnessError, "apk.version_code"):
                    artifact_verifier.verify_manifest(
                        manifest_path=manifest,
                        apk_path=apk,
                        payload_manifest_path=payload_manifest,
                        payload_artifact_manifest_path=payload_artifact_manifest,
                        payload_zip_path=payload_zip,
                        runtime_meta_path=runtime_meta,
                        install_stamp_path=install_stamp,
                        webui_revision_path=webui,
                        release_cert_path=cert,
                        aapt_path=root / "aapt",
                        signer_sha256="b" * 64,
                        repository=matrix.EXPECTED_REPOSITORY,
                        workflow="Build APK",
                        artifact_name="apk",
                        run_id="12345",
                        head_sha="c" * 40,
                    )

    def test_publish_verifier_rejects_noncanonical_apk_filename_before_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk = root / "other.apk"
            apk.write_bytes(b"synthetic APK bytes")
            artifact = matrix.build_artifact_manifest(
                apk,
                application_id=matrix.EXPECTED_APPLICATION_ID,
                version_name="0.1.2",
                version_code=99,
                signer_sha256="b" * 64,
                build_run_id="12345",
                head_sha="c" * 40,
                runtime_version="runtime-test",
                runtime_sha256="d" * 64,
                runtime_source_commit="e" * 40,
                payload_id="payload-test",
                webui_revision="f" * 40,
                repository=matrix.EXPECTED_REPOSITORY,
            )
            manifest = root / "artifact-manifest.json"
            manifest.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaisesRegex(matrix.HarnessError, "filename"):
                artifact_verifier.verify_manifest(
                    manifest_path=manifest,
                    apk_path=apk,
                    payload_manifest_path=root / "payload.json",
                    payload_artifact_manifest_path=root / "payload-artifact.json",
                    payload_zip_path=root / "payload.zip",
                    runtime_meta_path=root / "runtime-meta.json",
                    install_stamp_path=root / "install-stamp.json",
                    webui_revision_path=root / "webui-revision.txt",
                    release_cert_path=root / "release-cert.sha256",
                    aapt_path=root / "aapt",
                    signer_sha256="b" * 64,
                    repository=matrix.EXPECTED_REPOSITORY,
                    workflow=matrix.EXPECTED_WORKFLOW,
                    artifact_name=matrix.EXPECTED_ARTIFACT_NAME,
                    run_id="12345",
                    head_sha="c" * 40,
                )

    def test_publish_requires_the_same_actions_identity_and_releases_manifest(self):
        for required in (
            'run=$(gh api "repos/$GH_REPO/actions/runs/$RUN_ID")',
            'gh run download "$RUN_ID" --name apk --dir dist',
            "apks=(hermes-android-*.apk)",
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

    def test_publish_rebinds_every_manifest_component_to_downloaded_inputs(self):
        for required in (
            "scripts/verify_android_artifact_manifest.py",
            "validate_release_artifact",
            'gh run download "$RUN_ID" --name payload --dir payload-artifact',
            "--payload-manifest payload.json",
            "--payload-artifact-manifest ../payload-artifact/payload.json",
            "--payload-zip ../payload-artifact/payload.zip",
            "--runtime-meta runtime-meta.json",
            "--install-stamp install-stamp.json",
            "--webui-revision-file webui-revision.txt",
            '--signer-sha256 "$signer"',
            '--aapt "$aapt"',
            'apk_manifest["sha256"]',
            'apk_manifest["size_bytes"]',
            'for field in ("application_id", "version_name", "version_code")',
            'apk_manifest[field]',
            'apk_manifest["signer_sha256"]',
            'runtime["version"]',
            'runtime["sha256"]',
            'runtime["source_commit"]',
            'runtime["payload_id"]',
            '["webui_revision"]',
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.publish + self.builder + self.verifier)

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
