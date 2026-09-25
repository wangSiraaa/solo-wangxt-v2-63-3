"""
离线采集包接入端到端测试（真实 PostgreSQL/PostGIS）。

验收覆盖：
1. 重复包只生成一份业务结果（操作号幂等，重放返回原回执）；
2. 序号 1、3、2 到达时，3 待处理，补齐 2 后按 1→2→3 顺序执行；
3. 媒体上传中断重传：无孤儿证据、无重复扣分、无半个事件/候选；
4. 迟到整改/候选判定不反转已结案事件与已锁定处罚历史；
5. 业务拒绝（发生时无合同）可修复数据后重试，归属仍按发生时合同；
6. 旧直接上传接口与新接入共存，旧数据迁移后照常可用。
"""
import json
from datetime import datetime, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APITestCase

from assessment.mockimages import scene_png_bytes
from assessment.models import (
    CleaningContract,
    DuplicateCandidate,
    EvidencePhoto,
    IngestMedia,
    IngestOperation,
    PenaltyUnit,
    PenaltyVersion,
    ProblemEvent,
    Rectification,
    RoadGrid,
)

GRID1 = {
    "type": "Polygon",
    "coordinates": [[
        [121.4700, 31.2300], [121.4800, 31.2300],
        [121.4800, 31.2400], [121.4700, 31.2400],
        [121.4700, 31.2300],
    ]],
}
A_LNG, A_LAT = 121.4750, 31.2351
DEVICE = "PAD-001"


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
    def upload_media(self, media_id, scene, device=DEVICE, expect=201):
        upload = SimpleUploadedFile(f"{media_id}.png", scene_png_bytes(scene), content_type="image/png")
        resp = self.client.post(
            "/api/ingest/media/",
            {"media_id": media_id, "device_id": device, "image": upload},
            format="multipart",
        )
        self.assertEqual(resp.status_code, expect, resp.content)
        return resp.json()

    def report_op(self, op_no, seq, media_ids, occurred_at, lng=A_LNG, lat=A_LAT, **payload_extra):
        payload = {"lng": lng, "lat": lat, "photo_refs": list(media_ids), "category": "litter"}
        payload.update(payload_extra)
        return {
            "operation_no": op_no, "seq": seq, "op_type": "report",
            "occurred_at": occurred_at.isoformat(), "payload": payload,
        }

    def rectify_op(self, op_no, seq, occurred_at, *, event_no=None, report_operation_no=None,
                   media_ids=(), note=""):
        payload = {"note": note, "photo_refs": list(media_ids)}
        if event_no:
            payload["event_no"] = event_no
        if report_operation_no:
            payload["report_operation_no"] = report_operation_no
        return {
            "operation_no": op_no, "seq": seq, "op_type": "rectify",
            "occurred_at": occurred_at.isoformat(), "payload": payload,
        }

    def send_batch(self, ops, device=DEVICE, expect=(201, 200)):
        resp = self.client.post(
            "/api/ingest/batches/", {"device_id": device, "operations": ops}, format="json",
        )
        expected = (expect,) if isinstance(expect, int) else expect
        self.assertIn(resp.status_code, expected, resp.content)
        return resp.json()

    def get_operation(self, operation_id):
        resp = self.client.get(f"/api/ingest/operations/{operation_id}/")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    # ---------- 1. 重复包只生成一份业务结果 ----------
    def test_duplicate_batch_replay_returns_original_receipt(self):
        t = self.now
        self.upload_media("m-1", "scene_c_bins")
        op1 = self.report_op("op-1", 1, ["m-1"], t - timedelta(hours=2))

        first = self.send_batch([op1], expect=201)
        self.assertEqual(first["receipts"][0]["status"], "processed")
        self.assertFalse(first["receipts"][0]["replayed"])
        event_no = first["receipts"][0]["result"]["event_no"]
        penalty_no = first["receipts"][0]["result"]["penalty_no"]

        # 原样重放整个包：返回原回执，不产生任何新业务结果
        second = self.send_batch([op1], expect=200)
        self.assertTrue(second["receipts"][0]["replayed"])
        self.assertEqual(second["receipts"][0]["status"], "processed")
        self.assertEqual(second["receipts"][0]["result"]["event_no"], event_no)
        self.assertEqual(ProblemEvent.objects.count(), 1)
        self.assertEqual(PenaltyUnit.objects.count(), 1)
        self.assertEqual(EvidencePhoto.objects.count(), 1)
        self.assertEqual(PenaltyUnit.objects.get().penalty_no, penalty_no)

        # 同一操作号但内容被篡改 -> 409；同一序号被其他操作占用 -> 409
        tampered = self.report_op("op-1", 1, ["m-1"], t - timedelta(hours=3))
        self.send_batch([tampered], expect=409)
        seq_clash = self.report_op("op-other", 1, ["m-1"], t - timedelta(hours=2))
        self.send_batch([seq_clash], expect=409)
        self.assertEqual(ProblemEvent.objects.count(), 1)

    # ---------- 2. 序号 1、3、2 到达：3 待处理，补齐后按序执行 ----------
    def test_out_of_order_ops_wait_for_gap_then_execute_in_seq_order(self):
        t = self.now
        self.upload_media("m-1", "scene_c_bins")
        self.upload_media("m-2", "scene_a_angle1")
        op1 = self.report_op("op-1", 1, ["m-1"], t - timedelta(hours=3))
        op2 = self.report_op("op-2", 2, ["m-2"], t - timedelta(hours=2))
        # op3 整改 op2 上报的事件（设备本地引用，证明必须等 op2 先执行）
        op3 = self.rectify_op("op-3", 3, t - timedelta(hours=1), report_operation_no="op-2",
                              note="离线整改回执")

        r1 = self.send_batch([op1], expect=201)
        self.assertEqual(r1["receipts"][0]["status"], "processed")
        event_a_no = r1["receipts"][0]["result"]["event_no"]

        # 3 先于 2 到达：落账但待处理，绝不按到达顺序执行
        r3 = self.send_batch([op3], expect=201)
        self.assertEqual(r3["receipts"][0]["status"], "pending")
        self.assertEqual(Rectification.objects.count(), 0)
        self.assertEqual(ProblemEvent.objects.count(), 1)
        wm = r3["watermark"]
        self.assertEqual((wm["received_through"], wm["processed_through"]), (1, 1))

        # 补齐 2：先执行 2（立案），再执行 3（整改 2 的事件）
        r2 = self.send_batch([op2], expect=201)
        self.assertEqual(r2["receipts"][0]["status"], "processed")
        event_b_no = r2["receipts"][0]["result"]["event_no"]
        self.assertNotEqual(event_a_no, event_b_no)

        op3_detail = self.get_operation(r3["receipts"][0]["operation_id"])
        self.assertEqual(op3_detail["receipt"]["status"], "processed")
        self.assertEqual(op3_detail["receipt"]["result"]["event_no"], event_b_no)

        event_b = self.client.get(f"/api/events/{op3_detail['receipt']['result']['event_id']}/").json()
        self.assertEqual(event_b["status"], "rectified")
        # 整改提交时间取业务发生时间，而非到达时间
        submitted_at = datetime.fromisoformat(event_b["rectification"]["submitted_at"])
        self.assertEqual(submitted_at, t - timedelta(hours=1))
        event_a = ProblemEvent.objects.get(event_no=event_a_no)
        self.assertEqual(event_a.status, "open")

        # 水位连续推进到 3；设备状态查询一致
        device = self.client.get(f"/api/ingest/devices/{DEVICE}/").json()
        self.assertEqual((device["received_through"], device["processed_through"]), (3, 3))
        self.assertEqual(device["status_counts"]["processed"], 3)

    # ---------- 3. 媒体上传中断重传：无孤儿证据、无重复扣分 ----------
    def test_media_interruption_and_retry_leaves_no_orphans(self):
        t = self.now
        op1 = self.report_op("op-1", 1, ["m-late"], t - timedelta(hours=2))

        # 媒体尚未上传：操作落账待处理，不产生半个事件/处罚/候选/照片
        pending = self.send_batch([op1], expect=201)
        self.assertEqual(pending["receipts"][0]["status"], "pending")
        self.assertIn("m-late", pending["receipts"][0]["status_note"])
        for model in (EvidencePhoto, ProblemEvent, PenaltyUnit, DuplicateCandidate):
            self.assertEqual(model.objects.count(), 0, model.__name__)

        # 媒体补齐（模拟断点重传成功）：等待中的操作被唤醒执行
        media = self.upload_media("m-late", "scene_c_bins")
        self.assertFalse(media["reused"])
        detail = self.get_operation(pending["receipts"][0]["operation_id"])
        self.assertEqual(detail["receipt"]["status"], "processed")
        self.assertEqual(EvidencePhoto.objects.count(), 1)
        self.assertEqual(ProblemEvent.objects.count(), 1)
        self.assertEqual(PenaltyUnit.objects.count(), 1)

        # 再次重传同一媒体：幂等复用，不重复落盘、不重复物化
        again = self.upload_media("m-late", "scene_c_bins", expect=200)
        self.assertTrue(again["reused"])
        self.assertEqual(IngestMedia.objects.count(), 1)
        self.assertEqual(EvidencePhoto.objects.count(), 1)

        # 重放整个操作包：仍只有一份业务结果
        replay = self.send_batch([op1], expect=200)
        self.assertTrue(replay["receipts"][0]["replayed"])
        self.assertEqual(ProblemEvent.objects.count(), 1)
        self.assertEqual(PenaltyUnit.objects.count(), 1)
        self.assertEqual(PenaltyVersion.objects.count(), 1)

        # 同一媒体号但内容不同 -> 409
        self.upload_media("m-late", "scene_a_angle1", expect=409)

    def test_media_cannot_be_consumed_twice(self):
        t = self.now
        self.upload_media("m-shared", "scene_c_bins")
        op1 = self.report_op("op-1", 1, ["m-shared"], t - timedelta(hours=2))
        op2 = self.report_op("op-2", 2, ["m-shared"], t - timedelta(hours=1))
        r = self.send_batch([op1, op2], expect=201)
        self.assertEqual(r["receipts"][0]["status"], "processed")
        # 第二个操作引用同一媒体：拒绝重复立案，不产生第二份扣分
        self.assertEqual(r["receipts"][1]["status"], "rejected")
        self.assertEqual(r["receipts"][1]["error_code"], "IngestConflict")
        self.assertEqual(ProblemEvent.objects.count(), 1)
        self.assertEqual(PenaltyUnit.objects.count(), 1)
        self.assertEqual(EvidencePhoto.objects.count(), 1)

    # ---------- 4. 迟到整改/候选判定不反转已结案与锁定历史 ----------
    def test_late_rectify_and_candidate_decision_do_not_reverse_history(self):
        t = self.now
        self.upload_media("m-1", "scene_a_angle1")
        op1 = self.report_op("op-1", 1, ["m-1"], t - timedelta(hours=5))
        r1 = self.send_batch([op1], expect=201)
        event_id = r1["receipts"][0]["result"]["event_id"]
        event_no = r1["receipts"][0]["result"]["event_no"]

        # 正常整改回执（业务时间 t-3h）
        op2 = self.rectify_op("op-2", 2, t - timedelta(hours=3), event_no=event_no, note="已清理")
        r2 = self.send_batch([op2], expect=201)
        self.assertEqual(r2["receipts"][0]["status"], "processed")
        rectification_id = r2["receipts"][0]["result"]["rectification_id"]

        # 复核通过 -> 锁定当前处罚版本
        penalty = PenaltyUnit.objects.get(event_id=event_id)
        resp = self.client.post(f"/api/penalties/{penalty.id}/review/",
                                {"approved": True, "actor": "复核员-李"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["locked_version_no"], 1)

        # 迟到的重复整改回执（设备重发）：业务拒绝，结案状态与锁定历史不被反转
        op3 = self.rectify_op("op-3", 3, t - timedelta(hours=1), event_no=event_no, note="重复回执")
        r3 = self.send_batch([op3], expect=201)
        self.assertEqual(r3["receipts"][0]["status"], "rejected")
        self.assertEqual(r3["receipts"][0]["error_code"], "AlreadyRectified")
        self.assertEqual(Rectification.objects.count(), 1)
        rectification = Rectification.objects.get()
        self.assertEqual(rectification.id, rectification_id)
        self.assertEqual(rectification.submitted_at, t - timedelta(hours=3))
        penalty.refresh_from_db()
        self.assertEqual(penalty.status, "locked")
        self.assertEqual(penalty.locked_version.version_no, 1)
        self.assertEqual(PenaltyVersion.objects.count(), 1)

        # 旧接口补拍一张相似照片 -> 候选指向已整改事件：attach 被拒（400），不得反转结案
        upload = SimpleUploadedFile("repost.png", scene_png_bytes("scene_a_repost"),
                                    content_type="image/png")
        resp = self.client.post("/api/photos/", {
            "image": upload, "lng": A_LNG, "lat": A_LAT,
            "captured_at": (t - timedelta(minutes=30)).isoformat(), "note": "复查补拍",
        }, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        new_photo_id = resp.json()["id"]
        cand = DuplicateCandidate.objects.get(photo_id=new_photo_id, status="pending")
        resp = self.client.post(f"/api/candidates/{cand.id}/decide/",
                                {"action": "attach", "actor": "监督员-王"}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(ProblemEvent.objects.get(pk=event_id).status, "rectified")

        # 判定为复发：另立新事件（不触碰已结案事件与其锁定处罚）
        resp = self.client.post(f"/api/candidates/{cand.id}/decide/",
                                {"action": "create_new", "actor": "监督员-王",
                                 "note": "同一位置复发"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "recurrence")
        self.assertEqual(ProblemEvent.objects.count(), 2)
        self.assertEqual(PenaltyUnit.objects.count(), 2)
        self.assertEqual(ProblemEvent.objects.get(pk=event_id).status, "rectified")
        penalty.refresh_from_db()
        self.assertEqual(penalty.status, "locked")
        self.assertEqual(penalty.locked_version.version_no, 1)

    # ---------- 5. 业务拒绝 -> 修复数据 -> 重试；归属仍按发生时 ----------
    def test_rejected_op_retry_after_fix_keeps_occurred_at_attribution(self):
        t = self.now
        occurred = t - timedelta(days=500)  # 任何合同都不覆盖的时刻
        self.upload_media("m-1", "scene_c_bins")
        op1 = self.report_op("op-1", 1, ["m-1"], occurred)

        # 发生时无生效合同 -> 业务拒绝（不阻塞后续、不产生半个事件）
        r1 = self.send_batch([op1], expect=201)
        self.assertEqual(r1["receipts"][0]["status"], "rejected")
        self.assertEqual(r1["receipts"][0]["error_code"], "NoContractFound")
        self.assertEqual(ProblemEvent.objects.count(), 0)
        self.assertEqual(EvidencePhoto.objects.count(), 0)

        # 后续操作不受影响（rejected 是终态，不阻塞设备）
        self.upload_media("m-2", "scene_a_angle1")
        op2 = self.report_op("op-2", 2, ["m-2"], t - timedelta(hours=1))
        r2 = self.send_batch([op2], expect=201)
        self.assertEqual(r2["receipts"][0]["status"], "processed")

        # 补录覆盖发生时刻的合同后重试 -> 成功，归属按发生时合同
        CleaningContract.objects.create(
            code="A-ANCIENT", grid=self.grid1, contractor_name="上古保洁公司",
            valid_from=t - timedelta(days=600), valid_to=t - timedelta(days=400),
        )
        op_id = r1["receipts"][0]["operation_id"]
        resp = self.client.post(f"/api/ingest/operations/{op_id}/retry/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["status"], "processed")
        event = ProblemEvent.objects.get(event_no=resp.json()["result"]["event_no"])
        self.assertEqual(event.contractor_name, "上古保洁公司")
        self.assertEqual(event.occurred_at, occurred)
        self.assertEqual(event.penalty.contractor_name, "上古保洁公司")

        # 已处理的操作不能再重试（重放只能走批量接入拿原回执）
        resp = self.client.post(f"/api/ingest/operations/{op_id}/retry/")
        self.assertEqual(resp.status_code, 409)

    # ---------- 6. 旧直接上传接口与新接入共存 ----------
    def test_legacy_upload_and_ingest_coexist(self):
        t = self.now
        # 旧接口直接上传（迁移前的入口保持可用）
        upload = SimpleUploadedFile("old.png", scene_png_bytes("scene_a_angle1"),
                                    content_type="image/png")
        resp = self.client.post("/api/photos/", {
            "image": upload, "lng": A_LNG, "lat": A_LAT,
            "captured_at": (t - timedelta(hours=4)).isoformat(), "note": "旧接口直传",
        }, format="multipart")
        self.assertEqual(resp.status_code, 201, resp.content)
        old_photo_id = resp.json()["id"]

        # 新接入：相似照片经采集包立案，照样进入既有人工去重体系
        self.upload_media("m-1", "scene_a_angle2")
        op1 = self.report_op("op-1", 1, ["m-1"], t - timedelta(hours=3))
        r1 = self.send_batch([op1], expect=201)
        self.assertEqual(r1["receipts"][0]["status"], "processed")
        new_photo_id = r1["receipts"][0]["result"]["photo_ids"][0]

        # 跨链路候选：旧照片与新照片互为疑似重复，人工按位置/时间判定
        cand = DuplicateCandidate.objects.get(photo_id=new_photo_id, matched_photo_id=old_photo_id)
        resp = self.client.post(f"/api/candidates/{cand.id}/decide/",
                                {"action": "different", "actor": "监督员-王",
                                 "note": "不同问题"}, format="json")
        self.assertEqual(resp.status_code, 200)

        # 旧照片仍可走旧流程立案
        resp = self.client.post(f"/api/photos/{old_photo_id}/create_event/",
                                {"category": "litter"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(ProblemEvent.objects.count(), 2)
        self.assertEqual(PenaltyUnit.objects.count(), 2)

        # 状态查询：按设备/回执状态过滤
        resp = self.client.get(f"/api/ingest/operations/?device_id={DEVICE}&receipt__status=processed")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)
        resp = self.client.get(f"/api/ingest/operations/?device_id={DEVICE}&receipt__status=pending")
        self.assertEqual(resp.json()["count"], 0)

    # ---------- OpenAPI ----------
    def test_openapi_schema_includes_ingest_endpoints(self):
        resp = self.client.get("/api/schema/", HTTP_ACCEPT="application/vnd.oai.openapi+json")
        self.assertEqual(resp.status_code, 200)
        schema = json.loads(resp.content)
        for path in ["/api/ingest/batches/", "/api/ingest/media/",
                     "/api/ingest/operations/", "/api/ingest/operations/{id}/retry/",
                     "/api/ingest/devices/{device_id}/"]:
            self.assertIn(path, schema["paths"], path)
