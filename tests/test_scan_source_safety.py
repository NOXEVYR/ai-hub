"""Scan source failures preserve history; successful gallery sources prune deletions."""
import json
import os
from pathlib import Path
from unittest import mock

from aihub import api, images, jobs, scan
from test_aihub import Fixture


class ScanSourceSafety(Fixture):
    def test_unavailable_selected_model_source_is_rejected_before_replacing_index(self):
        source = self.ai / "20_Models"
        source.mkdir()
        model_file = source / "synthetic.safetensors"
        model_file.write_bytes(b"synthetic fixture only")
        self.cfg["scan_roots"] = [str(source)]

        result = scan.scan_all(self.db, self.cfg)
        scan.build_models(self.db, self.cfg, result)
        self.db.upsert_model({"path": str(model_file), "rating": 7, "notes": "keep annotation"})
        self.db.conn.execute("INSERT INTO model_labels(model_path,domain,purposes) VALUES(?,?,?)",
                             (str(model_file), "image", json.dumps(["style"])))
        self.db.commit()

        offline = self.ai / "20_Models.offline"
        source.rename(offline)
        with mock.patch.object(jobs, "run_full_pipeline") as start_job:
            status, _, raw = api.scan_start(self.db, self.cfg, {}, None)
        self.assertEqual(status, 409)
        self.assertIn("明确移除", json.loads(raw)["error"])
        start_job.assert_not_called()
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM files WHERE path=?", (str(model_file),))["n"], 1)
        row = self.db.model_by_path(str(model_file))
        self.assertEqual((row["missing"], row["rating"], row["notes"]), (0, 7, "keep annotation"))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM model_labels WHERE model_path=?",
                                     (str(model_file),))["n"], 1)

        # Removing the source from configuration is an explicit choice and may
        # remove its scan-table rows while retaining the personal model record.
        self.cfg["scan_roots"] = []
        empty_result = scan.scan_all(self.db, self.cfg)
        scan.build_models(self.db, self.cfg, empty_result)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM files WHERE path=?", (str(model_file),))["n"], 0)
        row = self.db.model_by_path(str(model_file))
        self.assertEqual((row["missing"], row["rating"], row["notes"]), (1, 7, "keep annotation"))
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM model_labels WHERE model_path=?",
                                     (str(model_file),))["n"], 1)

    def test_nested_directory_read_error_does_not_commit_partial_model_snapshot(self):
        source = self.ai / "20_Models"
        nested = source / "Nested"
        nested.mkdir(parents=True)
        old_file = source / "old.safetensors"
        old_file.write_bytes(b"old synthetic model")
        self.cfg["scan_roots"] = [str(source)]
        scan.scan_all(self.db, self.cfg)
        new_file = nested / "new.safetensors"
        new_file.write_bytes(b"new synthetic model")

        real_scandir = os.scandir

        def fail_nested(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(nested)):
                raise PermissionError("synthetic unreadable subtree")
            return real_scandir(path)

        with mock.patch("aihub.scan.os.scandir", side_effect=fail_nested):
            with self.assertRaisesRegex(scan.ScanSourceError, "读取扫描目录失败"):
                scan.scan_all(self.db, self.cfg)
        paths = {row["path"] for row in self.db.query("SELECT path FROM files")}
        self.assertIn(str(old_file), paths)
        self.assertNotIn(str(new_file), paths)

    def test_lstat_failure_does_not_become_an_excluded_model_subtree(self):
        self._assert_model_metadata_failure_preserves_history("lstat", "nested")

    def test_model_identity_stat_failure_does_not_mark_existing_model_missing(self):
        self._assert_model_metadata_failure_preserves_history("stat", "model")

    def _assert_model_metadata_failure_preserves_history(self, operation, target_kind):
        source = self.ai / "20_Models"
        nested = source / "Nested"
        nested.mkdir(parents=True)
        model = nested / "old.safetensors"
        model.write_bytes(b"synthetic model")
        self.cfg["scan_roots"] = [str(source)]
        result = scan.scan_all(self.db, self.cfg)
        scan.build_models(self.db, self.cfg, result)
        target = str(nested if target_kind == "nested" else model)
        original = getattr(os, operation)

        def fail_metadata(path, *args, **kwargs):
            if os.fspath(path) == target:
                raise PermissionError("synthetic metadata read failure")
            return original(path, *args, **kwargs)

        with mock.patch.object(os, operation, side_effect=fail_metadata):
            with self.assertRaises(scan.ScanSourceError):
                scan.scan_all(self.db, self.cfg)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM files WHERE path=?", (str(model),))["n"], 1)
        self.assertEqual(self.db.model_by_path(str(model))["missing"], 0)

    def test_gallery_prunes_only_completed_source_and_preserves_offline_history(self):
        good = self.ai / "output-current"
        offline = self.ai / "output-offline"
        untouched = self.ai / "output-not-selected"
        for root in (good, offline, untouched):
            root.mkdir()
        deleted_image = good / "deleted.png"
        offline_image = offline / "old.png"
        untouched_image = untouched / "old.png"
        offline_image.write_bytes(b"synthetic placeholder")
        untouched_image.write_bytes(b"synthetic placeholder")
        offline.rename(self.ai / "output-offline.disconnected")
        self.cfg["output_roots"] = [str(good), str(offline)]

        self.db.upsert_model({"path": "synthetic-model", "filename": "synthetic.safetensors",
                              "mtype": "LoRA", "rating": 6, "notes": "keep"})
        for image_path, parent in ((deleted_image, good), (offline_image, offline),
                                   (untouched_image, untouched)):
            self.db.conn.execute("INSERT INTO images(path,name,parent,mtime) VALUES(?,?,?,?)",
                                 (str(image_path), image_path.name, str(parent), 1))
            self.db.conn.execute("INSERT INTO img_refs(image_path,model_path,role,filename) VALUES(?,?,?,?)",
                                 (str(image_path), "synthetic-model", "LoRA", "synthetic.safetensors"))
        self.db.commit()

        stats = images.run_image_scan(self.db, self.cfg)
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(len(stats["failed_roots"]), 1)
        remaining = {row["path"] for row in self.db.query("SELECT path FROM images")}
        self.assertEqual(remaining, {str(offline_image), str(untouched_image)})
        refs = {row["image_path"] for row in self.db.query("SELECT image_path FROM img_refs")}
        self.assertEqual(refs, remaining)
        self.assertEqual(self.db.model_by_path("synthetic-model")["img_count"], 2)

    def test_partial_gallery_walk_error_keeps_source_rows_and_references(self):
        root = self.ai / "output-partial"
        root.mkdir()
        old_image = root / "historical.png"
        self.cfg["output_roots"] = [str(root)]
        self.db.upsert_model({"path": "synthetic-model", "filename": "synthetic.safetensors"})
        self.db.conn.execute("INSERT INTO images(path,name,parent,mtime) VALUES(?,?,?,?)",
                             (str(old_image), old_image.name, str(root), 1))
        self.db.conn.execute("INSERT INTO img_refs(image_path,model_path,role,filename) VALUES(?,?,?,?)",
                             (str(old_image), "synthetic-model", "LoRA", "synthetic.safetensors"))
        self.db.commit()

        def partial_walk(path, onerror=None, **kwargs):
            if onerror:
                onerror(PermissionError("synthetic subtree read failure"))
            yield str(root), [], []

        with mock.patch("aihub.images.os.walk", side_effect=partial_walk):
            stats = images.run_image_scan(self.db, self.cfg)
        self.assertEqual(len(stats["failed_roots"]), 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM images WHERE path=?", (str(old_image),))["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM img_refs WHERE image_path=?", (str(old_image),))["n"], 1)
        self.assertEqual(self.db.model_by_path("synthetic-model")["img_count"], 1)

    def test_full_pipeline_reports_partial_gallery_failure(self):
        missing_output = self.ai / "output-disconnected"
        self.cfg["output_roots"] = [str(missing_output)]
        captured = {}

        def capture_target(name, target, *args):
            captured["target"] = target

        with mock.patch.object(jobs, "start", side_effect=capture_target):
            jobs.run_full_pipeline(self.db, self.cfg)
        with self.assertRaisesRegex(RuntimeError, "图库扫描部分失败"):
            captured["target"]()

    def test_excluded_gallery_subtree_keeps_existing_image_history(self):
        root = self.ai / "output-current"
        excluded = root / ".archive"
        excluded.mkdir(parents=True)
        image = excluded / "still-here.png"
        image.write_bytes(b"synthetic placeholder")
        self.cfg["output_roots"] = [str(root)]
        self.db.upsert_model({"path": "synthetic-model", "filename": "synthetic.safetensors"})
        self.db.conn.execute("INSERT INTO images(path,name,parent,mtime) VALUES(?,?,?,?)",
                             (str(image), image.name, str(excluded), 1))
        self.db.conn.execute("INSERT INTO img_refs(image_path,model_path,role,filename) VALUES(?,?,?,?)",
                             (str(image), "synthetic-model", "LoRA", "synthetic.safetensors"))
        self.db.commit()

        stats = images.run_image_scan(self.db, self.cfg)
        self.assertEqual(stats["completed_roots"], [str(root)])
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM images WHERE path=?", (str(image),))["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM img_refs WHERE image_path=?", (str(image),))["n"], 1)

    def test_absent_excluded_gallery_subtrees_keep_history(self):
        root = self.ai / "output-current"
        root.mkdir()
        configured = root / "excluded-by-user"
        ignored = root / "ignored-by-name"
        self.cfg["output_roots"] = [str(root)]
        self.cfg["scan_exclude_paths"] = [str(configured)]
        self.cfg["ignore_dirs"] = [ignored.name]
        paths = [root / ".archive" / "old.png", configured / "old.png", ignored / "old.png"]
        for image in paths:
            self.db.conn.execute("INSERT INTO images(path,name,parent,mtime) VALUES(?,?,?,?)",
                                 (str(image), image.name, str(image.parent), 1))
        self.db.commit()
        stats = images.run_image_scan(self.db, self.cfg)
        self.assertEqual(stats["removed"], 0)
        self.assertEqual({r["path"] for r in self.db.query("SELECT path FROM images")}, set(map(str, paths)))

    def test_failed_nested_source_is_protected_from_completed_parent_prune(self):
        parent = self.ai / "output-parent"
        child = parent / "output-child"
        child.mkdir(parents=True)
        existing_child_image = child / "still-here.png"
        existing_child_image.write_bytes(b"synthetic placeholder")
        missing_child_image = child / "old-and-missing.png"
        missing_parent_image = parent / "old-and-missing.png"
        self.cfg["output_roots"] = [str(parent), str(child)]
        self.db.upsert_model({"path": "synthetic-model", "filename": "synthetic.safetensors"})
        for image in (existing_child_image, missing_child_image, missing_parent_image):
            self.db.conn.execute("INSERT INTO images(path,name,parent,mtime) VALUES(?,?,?,?)",
                                 (str(image), image.name, str(image.parent), 1))
            self.db.conn.execute("INSERT INTO img_refs(image_path,model_path,role,filename) VALUES(?,?,?,?)",
                                 (str(image), "synthetic-model", "LoRA", "synthetic.safetensors"))
        self.db.commit()

        parent_key = os.path.normcase(os.path.abspath(parent))
        child_key = os.path.normcase(os.path.abspath(child))

        def overlapping_walk(path, onerror=None, **kwargs):
            key = os.path.normcase(os.path.abspath(path))
            if key == parent_key:
                # Simulate the parent source completing while its nested source
                # is outside this walk's visible traversal.
                yield str(parent), [], []
            elif key == child_key:
                error = PermissionError(13, "synthetic child source unavailable", str(child))
                if onerror:
                    onerror(error)
                yield str(child), [], []
            else:
                self.fail(f"unexpected gallery source: {path}")

        with mock.patch("aihub.images.os.walk", side_effect=overlapping_walk):
            stats = images.run_image_scan(self.db, self.cfg)
        self.assertEqual(len(stats["failed_roots"]), 1)
        remaining = {row["path"] for row in self.db.query("SELECT path FROM images")}
        self.assertEqual(remaining, {str(existing_child_image), str(missing_child_image)})
        refs = {row["image_path"] for row in self.db.query("SELECT image_path FROM img_refs")}
        self.assertEqual(refs, remaining)

    def test_permission_error_during_missing_path_check_preserves_history(self):
        root = self.ai / "output-current"
        root.mkdir()
        image = root / "possibly-unreadable.png"
        self.cfg["output_roots"] = [str(root)]
        self.db.upsert_model({"path": "synthetic-model", "filename": "synthetic.safetensors"})
        self.db.conn.execute("INSERT INTO images(path,name,parent,mtime) VALUES(?,?,?,?)",
                             (str(image), image.name, str(root), 1))
        self.db.conn.execute("INSERT INTO img_refs(image_path,model_path,role,filename) VALUES(?,?,?,?)",
                             (str(image), "synthetic-model", "LoRA", "synthetic.safetensors"))
        self.db.commit()

        def empty_successful_walk(path, onerror=None, **kwargs):
            yield str(root), [], []

        real_stat = os.stat

        def guarded_stat(path, *args, **kwargs):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(image)):
                raise PermissionError("synthetic permission failure")
            return real_stat(path, *args, **kwargs)

        with mock.patch("aihub.images.os.walk", side_effect=empty_successful_walk), \
                mock.patch("aihub.images.os.stat", side_effect=guarded_stat):
            stats = images.run_image_scan(self.db, self.cfg)
        self.assertEqual(stats["completed_roots"], [str(root)])
        self.assertEqual(stats["removed"], 0)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM images WHERE path=?", (str(image),))["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM img_refs WHERE image_path=?", (str(image),))["n"], 1)


if __name__ == "__main__":
    import unittest
    unittest.main()
