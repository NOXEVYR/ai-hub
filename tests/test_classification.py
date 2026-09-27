import json
import os
from unittest import mock

from aihub import api, classification as c, config
from test_aihub import Fixture


class Classifier(Fixture):
    def test_domains_distinguish_media_and_unconfirmed_qwen_encoder(self):
        cases = [("LoRA", "anima", "image"), ("LoRA", "sdxl_base_v1-0", "image"),
                 ("LoRA", "Wan", "video"), ("Diffusion", "MiniMaxH3", "video"),
                 ("Diffusion", "MiniMaxMusic3", "audio"), ("LLM", "Qwen", "language"),
                 ("TextEncoder", "Qwen", "unknown"), ("LoRA", "QwenImage", "image"),
                 ("Vision", "SAM", "vision"), ("VideoAI", "Topaz", "video")]
        for kind, family, expected in cases:
            with self.subTest(kind=kind, family=family):
                result = c.classify({"mtype": kind, "family": family})
                self.assertEqual(result["domain"], expected)

    def test_component_filename_suggestions_do_not_certify_architecture(self):
        cases = [("minimax_h3_audio_vae_fp32.safetensors", "VAE", "audio"),
                 ("minimax_h3_video_vae_fp16.safetensors", "VAE", "video"),
                 ("qwen_image_vae.safetensors", "VAE", "image"),
                 ("SDXL-VAE-fp16.safetensors", "VAE", "image"),
                 ("wan2.1_vae.safetensors", "VAE", "video"),
                 ("stable_audio_vae.safetensors", "VAE", "audio"),
                 ("qwen_image_text_encoder.safetensors", "TextEncoder", "image"),
                 ("video_embedding.safetensors", "Embedding", "video")]
        for name, role, domain in cases:
            with self.subTest(name=name):
                result = c.classify({"mtype": role, "family": "unknown", "filename": name})
                self.assertEqual((result["domain"], result["domain_source"]), (domain, "filename"))
                self.assertIn("文件名", result["domain_evidence"])
                self.assertEqual((result["architecture"], result["architecture_source"]), ("未确认", "unknown"))
                self.assertEqual(result["model_role"], role)
                self.assertTrue(result["classification_pending"])

    def test_component_names_require_specific_unambiguous_evidence(self):
        names = ["vae.safetensors", "audio_visual_vae.safetensors", "audiovisual_vae.safetensors",
                 "videographer_vae.safetensors", "imageboard_vae.safetensors", "fluxurious_vae.safetensors",
                 "audio_vae_video.safetensors", "image_vae_video_vae.safetensors",
                 r"C:\video\vae.safetensors", "audio_vaeish.safetensors"]
        for name in names:
            with self.subTest(name=name):
                result = c.classify({"mtype": "VAE", "filename": name})
                self.assertEqual((result["domain"], result["domain_source"]), ("unknown", "unknown"))
                self.assertIn("不代表", result["domain_evidence"])
        # A LoRA's concept name is not evidence that it is an audio component.
        self.assertEqual(c.classify({"mtype": "LoRA", "filename": "audio_vae_style.safetensors"})["domain"], "unknown")

    def test_component_metadata_family_and_manual_precede_filename_hints(self):
        model = {"mtype": "VAE", "filename": "video_vae.safetensors", "family": "SDXL", "family_conf": "confirmed"}
        self.assertEqual(c.classify(model)["domain_source"], "architecture")
        self.assertEqual(c.classify(model)["domain"], "image")
        model["header_meta"] = {"modelspec.architecture": "stable-audio"}
        result = c.classify(model)
        self.assertEqual((result["domain"], result["domain_source"]), ("audio", "metadata"))
        result = c.classify(model, manual={"domain": "shared", "architecture": "reviewed-family"})
        self.assertEqual((result["domain"], result["domain_source"]), ("shared", "manual"))
        self.assertEqual((result["architecture"], result["architecture_source"]), ("reviewed-family", "manual"))

    def test_minimax_audio_component_is_not_the_family_video_workflow(self):
        model = {"mtype": "VAE", "filename": "minimax_h3_audio_vae_fp32.safetensors",
                 "family": "MiniMaxH3", "family_conf": "confirmed", "scope": "central"}
        result = c.classify(model)
        self.assertEqual((result["domain"], result["domain_source"]), ("audio", "filename"))
        self.assertEqual(result["architecture"], "MiniMaxH3")
        self.assertTrue(result["classification_pending"])
        self.assertIn("保留", result["domain_evidence"])
        result = c.classify(model, manual={"domain": "video"})
        self.assertEqual((result["domain"], result["domain_source"]), ("video", "manual"))
        # Explicit embedded component evidence wins a misleading filename.
        model["header_meta"] = {"modelspec.architecture": "minimax_h3_video_vae"}
        result = c.classify(model)
        self.assertEqual((result["domain"], result["domain_source"]), ("video", "metadata"))
        model["header_meta"] = {"modelspec.architecture": "minimax_h3_audio_vae"}
        self.assertEqual(c.classify(model)["domain"], "audio")
        result = c.classify({"mtype": "VAE", "filename": "vae.safetensors", "family": "minimax_h3_audio_vae", "family_conf": "confirmed"})
        self.assertEqual((result["domain"], result["domain_source"]), ("audio", "architecture"))

    def test_metadata_and_manual_choices_have_explicit_precedence(self):
        model = {"mtype": "LoRA", "family": "Unknown", "filename": "lighting-style.safetensors",
                 "header_meta": {"modelspec.architecture": "stable-diffusion-xl-v1-base/lora"}}
        auto = c.classify(model)
        self.assertEqual(auto["domain"], "image")
        self.assertEqual(auto["domain_source"], "metadata")
        self.assertEqual(set(auto["purposes"]), {"style", "lighting"})
        manual = c.classify(model, manual={"domain": "video", "purposes": '["motion"]'})
        self.assertEqual((manual["domain"], manual["purposes"]), ("video", ["motion"]))
        self.assertEqual(manual["purpose_source"], "manual")

    def test_lightning_is_acceleration_not_lighting_and_detail_beats_misfiled_folder(self):
        a = c.classify({"mtype": "LoRA", "filename": "sdxl_lightning_8step_lora.safetensors"})
        self.assertEqual(a["purposes"], ["acceleration"])
        b = c.classify({"mtype": "LoRA", "filename": "anima_context_detailer.safetensors"},
                       {"old_path": r"C:\AI\loras\加速\anima_context_detailer.safetensors"})
        self.assertEqual(b["purposes"], ["detail"])

    def test_legacy_catch_all_is_not_promoted_to_a_confirmed_style(self):
        model = {"mtype": "LoRA", "filename": "asset_123.safetensors",
                 "path": r"C:\AI\Library\Unknown\LoRA\Style_Other\asset_123.safetensors"}
        result = c.classify(model, {"role": "Style_Other"})
        self.assertEqual(result["domain"], "unknown")
        self.assertEqual(result["purposes"], ["uncategorized"])
        original = c.classify(model, {"old_path": r"C:\AI\loras\人物lora\游戏\asset_123.safetensors"})
        self.assertEqual(original["purposes"], ["character"])
        self.assertEqual(original["purpose_source"], "folder")

    def test_training_tag_frequencies_do_not_turn_a_style_into_a_character_lora(self):
        result = c.classify({"mtype": "LoRA", "filename": "brush_style.safetensors",
                             "header_meta": {"ss_tag_frequency": '{"data":{"1girl":500}}',
                                             "aihub.purposes": [{"not": "a category"}]}})
        self.assertEqual(result["purposes"], ["style"])


class CategoryAPI(Fixture):
    def add(self, name, family="SDXL", kind="LoRA", scope="central"):
        path = str(self.ai / name)
        self.db.upsert_model({"path": path, "filename": name, "mtype": kind,
                              "family": family, "scope": scope, "rating": 7, "notes": "keep my notes"})
        return self.db.model_by_path(path)["rowid_pk"]

    def payload(self, response):
        self.assertEqual(response[0], 200)
        return json.loads(response[2])

    def test_filters_apply_before_pagination_and_facets_keep_their_scope(self):
        for i in range(23):
            self.add(f"lighting-{i:02d}.safetensors")
        self.add("video-motion.safetensors", "Wan")
        self.add("language.gguf", "Qwen", "LLM")
        self.add("training_style.safetensors", scope="training")
        result = self.payload(api.models_list(self.db, self.cfg,
            {"domain": "image", "purpose": "lighting", "scope": "central", "page": "2", "size": "10", "sort": "name"}, None))
        self.assertEqual((result["total"], result["page"], len(result["items"])), (23, 2, 10))
        self.assertEqual(result["items"][0]["filename"], "lighting-10.safetensors")
        counts = {item["id"]: item["count"] for item in result["facets"]["domains"]}
        self.assertEqual((counts["image"], counts["video"], counts["language"]), (23, 1, 1))
        self.assertNotIn("Wan", result["facets"]["families"])

    def test_manual_changes_persist_across_scan_upserts_and_can_be_reset(self):
        mid = self.add("brush_style.safetensors")
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [mid], "domain": "image", "purposes": ["style", "lighting"]}))
        model = self.db.one("SELECT * FROM models WHERE rowid_pk=?", (mid,))
        self.db.upsert_model({"path": model["path"], "filename": "renamed.safetensors", "family": "Anima"})
        detail = self.payload(api.model_detail(self.db, self.cfg, {"id": mid}, None))
        self.assertEqual(detail["classification"]["purposes"], ["style", "lighting"])
        self.assertEqual((detail["rating"], detail["notes"]), (7, "keep my notes"))
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [mid], "reset": True}))
        detail = self.payload(api.model_detail(self.db, self.cfg, {"id": mid}, None))
        self.assertEqual(detail["classification"]["purposes"], ["uncategorized"])
        self.assertEqual(detail["notes"], "keep my notes")

    def test_component_domain_facets_and_manual_override_survive_rescan(self):
        audio = self.add("minimax_h3_audio_vae_fp32.safetensors", "unknown", "VAE")
        self.add("minimax_h3_video_vae_fp16.safetensors", "unknown", "VAE")
        self.add("qwen_image_vae.safetensors", "unknown", "VAE")
        self.add("vae.safetensors", "unknown", "VAE")
        result = self.payload(api.models_list(self.db, self.cfg, {}, None))
        counts = {item["id"]: item["count"] for item in result["facets"]["domains"]}
        self.assertEqual({key: counts.get(key, 0) for key in ("audio", "video", "image", "unknown", "shared")},
                         {"audio": 1, "video": 1, "image": 1, "unknown": 1, "shared": 0})
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [audio], "domain": "video"}))
        row = self.db.one("SELECT * FROM models WHERE rowid_pk=?", (audio,))
        self.db.upsert_model({"path": row["path"], "filename": row["filename"], "mtype": "VAE", "family": "unknown"})
        detail = self.payload(api.model_detail(self.db, self.cfg, {"id": audio}, None))
        self.assertEqual((detail["classification"]["domain"], detail["classification"]["domain_source"]), ("video", "manual"))
        self.assertEqual((detail["rating"], detail["notes"]), (7, "keep my notes"))
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [audio], "reset": True}))
        detail = self.payload(api.model_detail(self.db, self.cfg, {"id": audio}, None))
        self.assertEqual((detail["classification"]["domain"], detail["classification"]["domain_source"]), ("audio", "filename"))

    def test_bulk_updates_leave_unspecified_fields_and_non_lora_purposes_unchanged(self):
        a, b = self.add("style.safetensors"), self.add("base.safetensors", kind="Checkpoint")
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [a], "domain": "video"}))
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [a, b], "purposes": ["lighting"]}))
        ca = self.payload(api.model_detail(self.db, self.cfg, {"id": a}, None))["classification"]
        cb = self.payload(api.model_detail(self.db, self.cfg, {"id": b}, None))["classification"]
        self.assertEqual((ca["domain"], ca["purposes"]), ("video", ["lighting"]))
        self.assertEqual(cb["purposes"], [])
        self.payload(api.models_classify(self.db, self.cfg, {}, {"ids": [a], "domain": None}))
        ca = self.payload(api.model_detail(self.db, self.cfg, {"id": a}, None))["classification"]
        self.assertEqual((ca["domain"], ca["purposes"]), ("image", ["lighting"]))

    def test_invalid_bulk_request_is_atomic(self):
        mid = self.add("style.safetensors")
        bad = [{"ids": [mid, 99999], "domain": "image"}, {"ids": [mid], "domain": {}},
               {"ids": [mid], "purposes": "lighting"}, {"ids": [mid], "purposes": ["unknown-key"]},
               {"ids": [mid], "purposes": ["uncategorized", "style"]}, {"ids": [mid], "reset": "yes"},
               {"ids": [True], "domain": "image"}, {"ids": [mid]},
               {"ids": [mid], "domain": "image", "notes": "do not edit"}]
        for body in bad:
            with self.subTest(body=body):
                self.assertIn(api.models_classify(self.db, self.cfg, {}, body)[0], (400, 404))
                self.assertEqual(self.db.one("SELECT COUNT(*) n FROM model_labels")["n"], 0)

    def test_out_of_range_page_is_clamped_after_a_category_change(self):
        self.add("lighting.safetensors")
        result = self.payload(api.models_list(self.db, self.cfg, {"domain": "image", "purpose": "lighting", "page": "9"}, None))
        self.assertEqual((result["page"], len(result["items"])), (1, 1))


class FirstRun(Fixture):
    def test_fresh_config_does_not_assume_the_authors_drive(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = config.default_config()
        self.assertEqual((cfg["ai_root"], cfg["scan_roots"], cfg["output_roots"], cfg["catalog_dir"]), ("", [], [], ""))

    def test_detect_keeps_models_in_root_and_does_not_save_settings(self):
        (self.ai / "root.safetensors").write_bytes(b"fixture")
        (self.ai / "loras").mkdir()
        before = dict(self.cfg)
        with mock.patch.object(config, "save_config") as save:
            response = api.settings_detect(self.db, self.cfg, {}, {"ai_root": str(self.ai)})
        self.assertEqual(response[0], 200)
        result = json.loads(response[2])
        self.assertEqual(result["scan_roots"], [str(self.ai)])
        self.assertEqual(before, self.cfg)
        save.assert_not_called()

    def test_detect_rejects_nonexistent_or_relative_directories(self):
        for value in (None, [], "relative", str(self.folder / "missing")):
            self.assertEqual(api.settings_detect(self.db, self.cfg, {}, {"ai_root": value})[0], 400)
