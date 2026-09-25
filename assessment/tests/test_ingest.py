"""
采集包接入端到端测试（验收覆盖）：

1. 重复包只生成一份业务结果 —— 整包重放返回原回执，事件/处罚/证据不增加；
   同操作号不同内容、同序号不同操作均判冲突且账本不变；
2. 序号 1、3、2 到达 —— 3 保持待处理（不产生事件），补齐 2 后按 2→3 顺序执行；
3. 媒体上传中断重传 —— 媒体缺失时操作失败且不留半个事件/处罚/候选/证据；
   补齐媒体重试后只生成一份业务结果，重传媒体幂等，同键不同内容 409；
4. 迟到整改不反转已结案；迟到相似照片的候选判定不反转锁定历史；
5. 旧直接上传接口迁移后继续可用，且与采集包照片跨来源生成候选；
6. 整改回执可引用本设备立案操作，提交时间取业务发生时间；归属按发生时合同。
"""
import json
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APITestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    CleaningContract,
    CollectionDevice,
    DuplicateCandidate,
    EvidencePhoto,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    RoadGrid,
    StagedMedia,
)

GRID1 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4800, 31.2300],
        [121.4800, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
A1_LNG, A1_LAT = 121.4750, 31.2351


class IngestFlowTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.now = timezone.now().replace(microsecond=0)
        cls.grid1 = RoadGrid.objects.create(code="G1", name="人民东路一段", geom=json.dumps(GRID1))
        CleaningContract.objects.create(
            code="A-OLD", grid=cls.grid1, contractor_name="甲保洁公司",
            valid_from=cls.now - timedelta(days=400), valid_to=cls.now - timedelta(days=1),
        )
        CleaningContract.objects.create(
            code="B-NEW", grid=cls.grid1, contractor_name="乙保洁公司",
            valid_from=cls.now - timedelta(days=1), valid_to=cls.now + timedelta(days=300),
        )

    # ---------- 辅助 ----------
    def stage_media(self, device_no, media_key, scene, expect=(200, 201)):
        upload = SimpleUploadedFile(f"{media_key}.png", scene_png_bytes(scene),
                                    content_type="image/png")
        resp = self.client.post(
            "/api/ingest/media/",
            {"device_no": device_no, "media_key": media_key, "image": upload},
            format="multipart",
        )
        self.assertIn(resp.status_code, expect, resp.content)
        return resp

    def post_batch(self, device_no, operations, batch_no="", expect=201):
        resp = self.client.post(
            "/api/ingest/batches/",
            {"device_no": device_no, "batch_no": batch_no, "operations": operations},
            format="json",
        )
        self.assertEqual(resp.status_code, expect, resp.content)
        return resp.json()

    def report_op(self, op_id, seq, media_key, occurred_at, **payload_extra):
        payload = {"media_key": media_key, "category": "litter",
                   "lng": A1_LNG, "lat": A1_LAT, "uploader": "巡查员-张"}
        payload.update(payload_extra)
        return {"op_id": op_id, "seq": seq, "type": "photo_report",
                "occurred_at": occurred_at.isoformat(), "payload": payload}

    def rectify_op(self, op_id, seq, occurred_at, **payload):
        return {"op_id": op_id, "seq": seq, "type": "rectification",
                "occurred_at": occurred_at.isoformat(), "payload": payload}

    def receipt_of(self, device_no, op_id):
        resp = self.client.get(f"/api/ingest/receipts/?operation__device__device_no={device_no}")
        self.assertEqual(resp.status_code, 200)
        matches = [r for r in resp.json()["results"] if r["op_id"] == op_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def device_status(self, device_no):
        resp = self.client.get(f"/api/ingest/devices/{device_no}/")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    def business_counts(self):
        return {
            "photos": EvidencePhoto.objects.count(),
            "events": ProblemEvent.objects.count(),
            "penalties": PenaltyUnit.objects.count(),
            "versions": PenaltyVersion.objects.count(),
            "candidates": DuplicateCandidate.objects.count(),
        }

    # ---------- 1. 重复包只生成一份业务结果 ----------
    def test_duplicate_batch_replays_original_receipts(self):
        t = self.now
        self.stage_media("DEV-1", "m-1", "scene_a_angle1")
        op = self.report_op("op-1", 1, "m-1", t - timedelta(hours=3))

        first = self.post_batch("DEV-1", [op], batch_no="B1")
        item = first["receipts"][0]
        self.assertEqual(item["status"], "applied")
        self.assertFalse(item["replayed"])
        event_no = item["result"]["event_no"]
        penalty_no = item["result"]["penalty_no"]
        self.assertEqual(item["result"]["contractor_name"], "乙保洁公司")
        counts = self.business_counts()
        self.assertEqual((counts["events"], counts["penalties"], counts["photos"]), (1, 1, 1))

        # 整包重放：返回原回执（原结果），业务结果不增加
        second = self.post_batch("DEV-1", [op], batch_no="B1")
        replayed = second["receipts"][0]
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["status"], "applied")
        self.assertEqual(replayed["receipt_id"], item["receipt_id"])
        self.assertEqual(replayed["result"]["event_no"], event_no)
        self.assertEqual(replayed["result"]["penalty_no"], penalty_no)
        self.assertEqual(self.business_counts(), counts)
        self.assertEqual(PenaltyVersion.objects.count(), 1)  # 只有初版，无重复扣分

        # 同一操作号携带不同内容 -> 冲突，账本不变
        tampered = self.report_op("op-1", 1, "m-1", t - timedelta(hours=3), note="篡改")
        conflict = self.post_batch("DEV-1", [tampered])["receipts"][0]
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["error"]["code"], "op_id_redefined")
        self.assertEqual(self.business_counts(), counts)

        # 同一序号被其他操作号占用 -> 冲突
        seq_reused = self.report_op("op-9", 1, "m-1", t - timedelta(hours=3))
        conflict = self.post_batch("DEV-1", [seq_reused])["receipts"][0]
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["error"]["code"], "seq_reused")
        self.assertEqual(self.business_counts(), counts)

    # ---------- 2. 序号 1、3、2：补齐后按顺序执行 ----------
    def test_out_of_order_seq_applies_only_after_gap_filled(self):
        t = self.now
        for key in ("m-1", "m-2", "m-3"):
            self.stage_media("DEV-2", key, "scene_c_bins")
        op1 = self.report_op("op-1", 1, "m-1", t - timedelta(hours=3))
        op2 = self.report_op("op-2", 2, "m-2", t - timedelta(hours=2))
        op3 = self.report_op("op-3", 3, "m-3", t - timedelta(hours=1))

        # 1、3 先到：1 执行，3 因缺失前序 2 保持待处理（不产生事件）
        first = self.post_batch("DEV-2", [op1, op3])
        by_op = {r["op_id"]: r for r in first["receipts"]}
        self.assertEqual(by_op["op-1"]["status"], "applied")
        self.assertEqual(by_op["op-3"]["status"], "pending")
        self.assertEqual(ProblemEvent.objects.count(), 1)
        status = self.device_status("DEV-2")
        self.assertEqual((status["received_watermark"], status["applied_watermark"]), (1, 1))
        self.assertEqual((status["applied_count"], status["pending_count"]), (1, 1))

        # 补齐 2：按 2→3 顺序执行（事件创建顺序与序号一致）
        second = self.post_batch("DEV-2", [op2])
        self.assertEqual(second["receipts"][0]["status"], "applied")
        r3 = self.receipt_of("DEV-2", "op-3")
        self.assertEqual(r3["status"], "applied")
        self.assertEqual(ProblemEvent.objects.count(), 3)
        event2_id = self.receipt_of("DEV-2", "op-2")["result"]["event_id"]
        self.assertLess(event2_id, r3["result"]["event_id"])
        status = self.device_status("DEV-2")
        self.assertEqual((status["received_watermark"], status["applied_watermark"]), (3, 3))
        self.assertEqual(status["pending_count"], 0)

    # ---------- 3. 媒体上传中断重传：无孤儿证据、无重复扣分 ----------
    def test_media_retry_after_interrupted_upload(self):
        t = self.now
        # 操作先到、媒体尚未上传（上传中断）-> 失败，且不留半个业务结果
        op = self.report_op("op-1", 1, "m-1", t - timedelta(hours=3))
        first = self.post_batch("DEV-3", [op])
        item = first["receipts"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["error"]["code"], "media_not_available")
        self.assertEqual(self.business_counts(),
                         {"photos": 0, "events": 0, "penalties": 0,
                          "versions": 0, "candidates": 0})
        self.assertEqual(self.device_status("DEV-3")["applied_watermark"], 0)

        # 补齐媒体（重传）后重试 -> 成功，恰好一份业务结果
        self.stage_media("DEV-3", "m-1", "scene_a_angle1")
        resp = self.client.post(f"/api/ingest/receipts/{item['receipt_id']}/retry/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "applied")
        self.assertEqual(resp.json()["attempts"], 2)
        counts = self.business_counts()
        self.assertEqual((counts["photos"], counts["events"], counts["penalties"]), (1, 1, 1))
        self.assertEqual(PenaltyVersion.objects.count(), 1)
        media = StagedMedia.objects.get(device__device_no="DEV-3", media_key="m-1")
        self.assertEqual(media.status, "consumed")
        self.assertIsNotNone(media.consumed_by_id)

        # 媒体重传幂等：同键同内容返回原记录
        resp = self.stage_media("DEV-3", "m-1", "scene_a_angle1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["id"], media.id)
        self.assertEqual(StagedMedia.objects.count(), 1)
        # 同键不同内容 -> 409
        self.stage_media("DEV-3", "m-1", "scene_c_bins", expect=(409,))
        # 操作重放 -> 原回执，无重复扣分
        replay = self.post_batch("DEV-3", [op])["receipts"][0]
        self.assertTrue(replay["replayed"])
        self.assertEqual(self.business_counts(), counts)
        self.assertEqual(PenaltyVersion.objects.count(), 1)
        # 非失败状态不允许重试
        resp = self.client.post(f"/api/ingest/receipts/{item['receipt_id']}/retry/")
        self.assertEqual(resp.status_code, 409)

    def test_domain_failure_rolls_back_all_writes(self):
        t = self.now
        # 位置在海里：归属失败（无合同）-> 照片/事件/处罚/候选全部回滚，媒体不被消费
        self.stage_media("DEV-4", "m-1", "scene_a_angle1")
        op = self.report_op("op-1", 1, "m-1", t - timedelta(hours=3), lng=10.0, lat=10.0)
        item = self.post_batch("DEV-4", [op])["receipts"][0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["error"]["code"], "no_contract_found")
        self.assertEqual(self.business_counts(),
                         {"photos": 0, "events": 0, "penalties": 0,
                          "versions": 0, "candidates": 0})
        media = StagedMedia.objects.get(device__device_no="DEV-4", media_key="m-1")
        self.assertEqual(media.status, "staged")  # 媒体未被消费，可修正后重试

    # ---------- 4. 迟到整改/候选判定不反转已结案或锁定历史 ----------
    def test_late_rectification_and_decisions_do_not_reverse_closed_or_locked(self):
        t = self.now
        # 设备立案：发生在 50 小时前
        self.stage_media("DEV-5", "m-1", "scene_a_angle1")
        op1 = self.report_op("op-1", 1, "m-1", t - timedelta(hours=50))
        result = self.post_batch("DEV-5", [op1])["receipts"][0]["result"]
        event_id, event_no = result["event_id"], result["event_no"]
        penalty_id = PenaltyUnit.objects.get(event_id=event_id).id

        # 逾期升级 L1（注入时钟）并复核锁定 v2
        run = self.client.post("/api/escalations/run/",
                               {"now": (t - timedelta(hours=25)).isoformat()}, format="json")
        self.assertEqual(run.json()["created_count"], 1)
        review = self.client.post(f"/api/penalties/{penalty_id}/review/",
                                  {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(review.json()["locked_version_no"], 2)

        # 在线渠道先行整改（事件结案）
        online_rectify = self.client.post(
            f"/api/events/{event_id}/rectify/",
            {"actor": "乙班组长", "note": "在线已清理",
             "now": (t - timedelta(hours=1)).isoformat()}, format="json")
        self.assertEqual(online_rectify.status_code, 201)
        rectification_id = online_rectify.json()["rectification"]["id"]

        # 设备迟到的整改回执（业务时间更早但到达晚）-> 已忽略，不反转结案
        op2 = self.rectify_op("op-2", 2, t - timedelta(hours=30),
                              event_no=event_no, note="离线补录整改")
        item = self.post_batch("DEV-5", [op2])["receipts"][0]
        self.assertEqual(item["status"], "ignored")
        self.assertEqual(item["result"]["rectification_id"], rectification_id)
        event = self.client.get(f"/api/events/{event_id}/").json()
        self.assertEqual(event["status"], "rectified")
        self.assertEqual(event["rectification"]["id"], rectification_id)
        self.assertEqual(event["rectification"]["note"], "在线已清理")
        self.assertEqual(Rectification.objects.count(), 1)
        # 设备水位照常推进（忽略视为已处理，不阻塞后续）
        self.assertEqual(self.device_status("DEV-5")["applied_watermark"], 2)

        # 设备迟到的相似照片立案 -> 产生人工候选（采集照片仍进入既有人工去重）
        self.stage_media("DEV-5", "m-2", "scene_a_repost")
        op3 = self.report_op("op-3", 3, "m-2", t - timedelta(hours=2))
        result3 = self.post_batch("DEV-5", [op3])["receipts"][0]["result"]
        self.assertEqual(len(result3["candidate_ids"]), 1)
        # 人工对该候选判 different：只标记不合并，不影响任何事件/处罚
        resp = self.client.post(f"/api/candidates/{result3['candidate_ids'][0]}/decide/",
                                {"action": "different", "actor": "监督员-王",
                                 "note": "位置相近但非同一堆"}, format="json")
        self.assertEqual(resp.status_code, 200)

        # 旧接口直传一张与首报完全相同的图（无事件）-> 候选匹配到已结案事件的采集照片
        upload = SimpleUploadedFile("late.png", scene_png_bytes("scene_a_elsewhere_copy"),
                                    content_type="image/png")
        resp = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": A1_LNG, "lat": A1_LAT,
             "captured_at": (t - timedelta(minutes=90)).isoformat()},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        late_photo_id = resp.json()["id"]
        op1_photo_id = result["photo_id"]
        candidate = DuplicateCandidate.objects.get(
            photo_id=late_photo_id, matched_photo_id=op1_photo_id, status="pending")

        # 人工判 attach -> 400：被匹配事件已整改结案，不能挂接反转
        resp = self.client.post(f"/api/candidates/{candidate.id}/decide/",
                                {"action": "attach", "actor": "监督员-王"}, format="json")
        self.assertEqual(resp.status_code, 400)
        # 人工判 create_new -> 复发：新事件新处罚；已结案事件与锁定处罚历史不反转
        resp = self.client.post(f"/api/candidates/{candidate.id}/decide/",
                                {"action": "create_new", "actor": "监督员-王"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "recurrence")
        old_penalty = self.client.get(f"/api/penalties/{penalty_id}/").json()
        self.assertEqual(old_penalty["status"], "locked")
        self.assertEqual(old_penalty["locked_version_no"], 2)
        self.assertEqual(len(old_penalty["versions"]), 2)
        self.assertEqual(float(old_penalty["points"]), 3.0)
        event = self.client.get(f"/api/events/{event_id}/").json()
        self.assertEqual(event["status"], "rectified")
        new_penalty = PenaltyUnit.objects.get(event__primary_photo_id=late_photo_id)
        self.assertEqual(new_penalty.status, "draft")
        self.assertEqual(new_penalty.versions.count(), 1)

    # ---------- 5. 旧直接上传接口与跨来源去重 ----------
    def test_legacy_direct_upload_and_cross_source_dedup_still_work(self):
        t = self.now
        # 旧接口直传（迁移前已有的使用方式）
        upload = SimpleUploadedFile("legacy.png", scene_png_bytes("scene_a_angle1"),
                                    content_type="image/png")
        resp = self.client.post(
            "/api/photos/",
            {"image": upload, "lng": A1_LNG, "lat": A1_LAT,
             "captured_at": (t - timedelta(hours=4)).isoformat(), "note": "旧接口"},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        legacy_photo = resp.json()

        # 采集包上传相似照片 -> 跨来源候选（人工去重覆盖两种来源）
        self.stage_media("DEV-6", "m-1", "scene_a_repost")
        op = self.report_op("op-1", 1, "m-1", t - timedelta(hours=3))
        result = self.post_batch("DEV-6", [op])["receipts"][0]["result"]
        self.assertEqual(len(result["candidate_ids"]), 1)
        candidate = DuplicateCandidate.objects.get(pk=result["candidate_ids"][0])
        self.assertEqual(candidate.matched_photo_id, legacy_photo["id"])
        self.assertEqual(candidate.status, "pending")

        # 旧流程立案仍可用：直传照片判 different 后单独立案，归属发生时合同
        resp = self.client.post(f"/api/candidates/{candidate.id}/decide/",
                                {"action": "different", "actor": "监督员-王"}, format="json")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f"/api/photos/{legacy_photo['id']}/create_event/",
                                {"category": "litter"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["contractor_name"], "乙保洁公司")
        self.assertEqual(PenaltyUnit.objects.count(), 2)  # 各自独立处罚

    # ---------- 6. 整改回执引用立案操作 + 业务发生时间 + 发生时归属 ----------
    def test_rectification_references_report_and_uses_business_time(self):
        t = self.now
        self.stage_media("DEV-7", "m-1", "scene_a_angle1")
        self.stage_media("DEV-7", "m-2", "scene_a_angle2")
        # 发生在 200 天前（甲公司区间），整改提交于 5 小时前
        op1 = self.report_op("op-1", 1, "m-1", t - timedelta(days=200))
        op2 = self.rectify_op("op-2", 2, t - timedelta(hours=5),
                              report_op_id="op-1", media_key="m-2",
                              note="已清理", actor="甲班组长")
        batch = self.post_batch("DEV-7", [op1, op2])
        by_op = {r["op_id"]: r for r in batch["receipts"]}
        self.assertEqual(by_op["op-1"]["status"], "applied")
        self.assertEqual(by_op["op-2"]["status"], "applied")
        # 归属按业务发生时间（200 天前）-> 甲公司，与接收时间无关
        self.assertEqual(by_op["op-1"]["result"]["contractor_name"], "甲保洁公司")

        event = self.client.get(f"/api/events/{by_op['op-1']['result']['event_id']}/").json()
        self.assertEqual(event["status"], "rectified")
        rectification = event["rectification"]
        # 整改提交时间取业务发生时间，而不是服务器接收时间
        self.assertEqual(parse_datetime(rectification["submitted_at"]), t - timedelta(hours=5))
        self.assertEqual(rectification["submitted_by"], "甲班组长")
        # 整改照片转为证据并挂到事件
        self.assertIsNotNone(rectification["photo"])
        self.assertEqual({ph["id"] for ph in event["photos"]},
                         {by_op["op-1"]["result"]["photo_id"], rectification["photo"]})

    # ---------- OpenAPI ----------
    def test_openapi_includes_ingest_paths(self):
        resp = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        for path in ["/api/ingest/batches/", "/api/ingest/media/",
                     "/api/ingest/devices/{device_no}/", "/api/ingest/receipts/",
                     "/api/ingest/receipts/{id}/retry/"]:
            self.assertIn(path, schema["paths"], path)
