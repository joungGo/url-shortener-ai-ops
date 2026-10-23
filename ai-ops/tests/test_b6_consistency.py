"""S9의 실제 경로 판정 경계와 부분 실패 뒤 자기 데이터 복구를 검사한다."""

from copy import deepcopy
import unittest
from unittest.mock import Mock

from ai_ops.b6_consistency import check_consistency_round
from ai_ops.b6_scenarios import execute


def observed_round(index):
    counters = {}; count = None; observations = []
    for phase, outcome, marker, cache_before, cache_after in (
        ("recent_write", "consistency_primary", 4500, -2, 60000),
        ("marker_expired_cache_hit", "hit", -2, 54000, 54000),
        ("cache_expired_read_session", "miss", -2, -2, 60000),
        ("warm_cache_hit", "hit", -2, 59000, 59000),
    ):
        before, old_count = deepcopy(counters), count
        counters[outcome] = counters.get(outcome, 0) + 1
        if outcome != "hit": count = (count or 0) + 1
        observations.append({"phase": phase, "http_status": 200, "marker_before_ms": marker,
            "marker_after_ms": marker, "cache_before_ms": cache_before, "cache_after_ms": cache_after,
            "counters_before": before, "counters_after": deepcopy(counters),
            "db_count_before": old_count, "db_count_after": count})
    return {"round": index, "observations": observations, "marker_ttl_seconds": 5,
        "natural_list_cache_expiry": True, "bypass_header_sent": False, "delay_header_sent": False,
        "redis_ping": True, "http_calls": 11 if index == 0 else 10,
        "database_roles": {"write_in_recovery": False, "read_in_recovery": False,
                           "same_configured_database": True} if index == 0 else None}


class ConsistencyLab:
    def __init__(self, fail=None, report=None):
        self.fail = fail; self.actions = []
        self.report = report if report is not None else {"hypotheses": 0, "current_state": "unknown",
            "fact_evidence_ids": [["ev-metric-cache_outcomes"]],
            "metric_samples": {"cache_outcomes": [{"outcome": "consistency_primary", "value": 1.04}]}}

    def production(self): return {"production": "unchanged"}

    def guard(self):
        if self.fail == "guard": raise ValueError("scrape insufficient")
        return {"lab-api": {"uid": "same", "generation": 1, "image": "fixed"}}

    def consistency(self, action, run_id):
        self.actions.append(action)
        if self.fail == action: raise ValueError("response lost")
        if action == "cleanup":
            return {"owned_data_removed": True, "markers_expired": True, "redis_ping": True, "http_calls": 7}
        return observed_round(int(action[-1]))

    def collect_consistency(self, run_id):
        self.actions.append("collect")
        if self.fail == "collect": raise ValueError("missing source")
        return self.report


class B6ConsistencyTest(unittest.TestCase):
    def run_s9(self, lab, **kwargs):
        return execute("S9", lab, run_id="1"*32, sleep=lambda _: None, **kwargs)

    def test_marker_expiry_hit_then_natural_cache_expiry_db_read_is_valid(self):
        check_consistency_round(observed_round(0))
        lab = ConsistencyLab(); saved = []
        def checkpoint(value):
            self.assertNotIn("collect", lab.actions)
            saved.append(value)
        result = self.run_s9(lab, checkpoint=checkpoint)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["http_calls"], 28)
        self.assertTrue(all(result["cleanup"].values()))
        self.assertEqual(saved[0]["status"], "observations_recorded")
        self.assertFalse(saved[0]["execution_status_final"])

    def test_unexpired_marker_fake_hit_and_db_queries_during_hit_are_rejected(self):
        for index, changes in ((0, {"marker_after_ms": -2}), (1, {"marker_before_ms": 1}),
                               (1, {"cache_before_ms": -2}), (1, {"db_count_after": 2}),
                               (2, {"cache_before_ms": 100}), (2, {"db_count_after": 1})):
            with self.subTest(index=index, changes=changes), self.assertRaises(ValueError):
                value = observed_round(0); value["observations"][index].update(changes)
                check_consistency_round(value)

    def test_cache_error_extra_results_and_missing_counters_cannot_pass(self):
        for changes in ({"error": 1}, {"consistency_primary": 0}, {"consistency_primary": 1, "miss": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                value = observed_round(0); value["observations"][0]["counters_after"] = changes
                check_consistency_round(value)

    def test_replica_role_or_forced_headers_cannot_be_claimed_as_single_primary(self):
        for changes in ({"database_roles": {"write_in_recovery": False, "read_in_recovery": True}},
                        {"bypass_header_sent": True}, {"natural_list_cache_expiry": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                check_consistency_round({**observed_round(0), **changes})

    def test_lost_creation_response_second_round_or_collection_still_removes_owned_data(self):
        for failure in ("round-0", "round-1", "collect"):
            with self.subTest(failure=failure):
                lab = ConsistencyLab(fail=failure); result = self.run_s9(lab)
                self.assertEqual(result["status"], "failed")
                self.assertIn("cleanup", lab.actions)
                self.assertTrue(all(result["cleanup"].values()))

    def test_bad_preflight_never_writes(self):
        lab = ConsistencyLab(fail="guard")
        self.assertEqual(self.run_s9(lab)["status"], "blocked")
        self.assertEqual(lab.actions, [])

    def test_cleanup_failure_blocks_following_scenario(self):
        result = self.run_s9(ConsistencyLab(fail="cleanup"))
        self.assertEqual(result["status"], "recovery_failed")
        self.assertFalse(result["cleanup"]["owned_data_removed"])

    def test_missing_metric_citation_false_cause_or_healthy_claim_requires_review(self):
        for changes in ({"hypotheses": 1}, {"current_state": "healthy"},
                        {"fact_evidence_ids": []}, {"metric_samples": {}},
                        {"metric_samples": {"cache_outcomes": [{"outcome": "consistency_primary", "value": 0}]}}):
            with self.subTest(changes=changes):
                lab = ConsistencyLab(); lab.report.update(changes)
                self.assertEqual(self.run_s9(lab)["status"], "needs_review")

    def test_request_budget_exceeded_is_not_success(self):
        lab = ConsistencyLab(); original = lab.consistency
        def excessive(action, run):
            value = original(action, run)
            if action == "cleanup": value["http_calls"] = 8
            return value
        lab.consistency = excessive
        result = self.run_s9(lab)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_reason"], "request_budget_exceeded")

    def test_checkpoint_failure_does_not_start_analysis_and_still_cleans_up(self):
        lab = ConsistencyLab(); result = self.run_s9(lab, checkpoint=Mock(side_effect=ValueError("disk full")))
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("collect", lab.actions)
        self.assertTrue(result["cleanup"]["owned_data_removed"])
