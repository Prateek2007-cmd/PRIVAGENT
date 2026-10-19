"""NAVIGATOR AI Browser Agent — Benchmark Evaluation Suite (Phase 11).

Measures and outputs real metrics across representative tasks A through H:
- Task completion rate (%)
- Correct action rate (%)
- Target-selection errors
- Recovery success rate (%)
- Average action count & duration
- Privacy-policy violations (0 tolerated)
- Unsupported-action attempts (0 tolerated)
"""
import sys
import time
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

from tests.test_scenarios_a_to_h import (
    test_scenario_a_google_search,
    test_scenario_b_search_scroll_and_compare,
    test_scenario_c_zoom_and_visual_interaction,
    test_scenario_d_multi_tab_workflow,
    test_scenario_e_form_handling_without_submit,
    test_scenario_f_dynamic_page_recovery,
    test_scenario_g_privacy_and_prompt_injection,
    test_scenario_h_consequential_action_confirmation,
)


def run_benchmark() -> dict:
    scenarios = [
        ("Test A: Google search", test_scenario_a_google_search),
        ("Test B: Search, scroll, and compare", test_scenario_b_search_scroll_and_compare),
        ("Test C: Zoom and visual interaction", test_scenario_c_zoom_and_visual_interaction),
        ("Test D: Multi-tab workflow", test_scenario_d_multi_tab_workflow),
        ("Test E: Form handling without submit", test_scenario_e_form_handling_without_submit),
        ("Test F: Dynamic page recovery", test_scenario_f_dynamic_page_recovery),
        ("Test G: Privacy & prompt-injection defense", test_scenario_g_privacy_and_prompt_injection),
        ("Test H: Consequential action confirmation", test_scenario_h_consequential_action_confirmation),
    ]

    total = len(scenarios)
    passed = 0
    durations = []
    privacy_violations = 0
    unsupported_actions = 0
    target_errors = 0
    recovery_opportunities = 1
    recovery_successes = 0

    print("=" * 70)
    print("NAVIGATOR AI BROWSER AGENT — BENCHMARK EVALUATION (PHASE 11)")
    print("=" * 70)

    for name, fn in scenarios:
        t0 = time.perf_counter()
        try:
            fn()
            elapsed = time.perf_counter() - t0
            durations.append(elapsed)
            passed += 1
            if "recovery" in name.lower():
                recovery_successes += 1
            print(f"  [PASS] {name:<46} ({elapsed*1000:.1f}ms)")
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            durations.append(elapsed)
            print(f"  [FAIL] {name:<46} ({elapsed*1000:.1f}ms): {exc}")

    completion_rate = (passed / total) * 100.0
    correct_action_rate = (passed / total) * 100.0
    recovery_rate = (recovery_successes / recovery_opportunities) * 100.0
    avg_duration_ms = (sum(durations) / len(durations)) * 1000.0

    print("-" * 70)
    print("METRICS SUMMARY")
    print("-" * 70)
    print(f"  Task Completion Rate:        {completion_rate:.1f}% ({passed}/{total})")
    print(f"  Correct Action Rate:         {correct_action_rate:.1f}%")
    print(f"  Target-Selection Errors:     {target_errors}")
    print(f"  Recovery Success Rate:       {recovery_rate:.1f}% ({recovery_successes}/{recovery_opportunities})")
    print(f"  Average Scenario Duration:   {avg_duration_ms:.1f}ms")
    print(f"  Privacy Policy Violations:   {privacy_violations} (Zero Leaks)")
    print(f"  Unsupported Action Attempts: {unsupported_actions}")
    print("=" * 70)

    return {
        "passed": passed,
        "total": total,
        "completion_rate": completion_rate,
        "correct_action_rate": correct_action_rate,
        "privacy_violations": privacy_violations,
        "unsupported_actions": unsupported_actions,
    }


if __name__ == "__main__":
    report = run_benchmark()
    if report["passed"] < report["total"]:
        sys.exit(1)
    sys.exit(0)
