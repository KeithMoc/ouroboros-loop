# Parking-Lot Audit v2 — Compounding vs Parallel

Controlled experiment with 8-AC chain and **deliberately ambiguous** representation choices. AC-1 picks 4 axes (timestamp format, tag rep, entry-id format, soft-delete semantics); AC-7 picks a 5th (priority encoding). Downstream ACs must align with those picks. The audit measures alignment.

## Headline (means across replays)

| Metric | Parallel | Compounding | Δ |
|--------|---------:|------------:|--:|
| n | 1.00 | 1.00 | +0.00 |
| ac_count_mean | 6.00 | 8.00 | +2.00 |
| wall_seconds_mean | 1494.00 | 2111.00 | +617.00 |
| tool_calls_mean | 181.00 | 148.00 | -33.00 |
| messages_mean | 635.00 | 223.00 | -412.00 |
| tokens_mean | 0.00 | 0.00 | +0.00 |
| cost_mean | 0.00 | 0.00 | +0.00 |
| alignment_total_mean | 5.00 | 5.00 | +0.00 |
| axis_1_aligned_rate | 1.00 | 1.00 | +0.00 |
| axis_2_aligned_rate | 1.00 | 1.00 | +0.00 |
| axis_3_aligned_rate | 1.00 | 1.00 | +0.00 |
| axis_4_aligned_rate | 1.00 | 1.00 | +0.00 |
| axis_5_aligned_rate | 1.00 | 1.00 | +0.00 |
| schema_field_coverage_mean | 10.00 | 10.00 | +0.00 |
| invariant_count_mean | 93.00 | 70.00 | -23.00 |
| tests_passed_mean | 130.00 | 106.00 | -24.00 |
| tests_failed_mean | 4.00 | 5.00 | +1.00 |
| smoke_ok_rate | 0.00 | 0.00 | +0.00 |

## Per-run

| Run | ACs | Wall(s) | Tools | Msgs | Align(0-5) | Schema(0-10) | Invariants | Tests p/f | Smoke | AC-1 picks |
|-----|----:|--------:|------:|-----:|-----------:|-------------:|-----------:|----------:|------:|------------|
| compounding-r1 | 8 | 2111 | 148 | 223 | 5/5 | 10/10 | 70 | 106/5 | ❌ | 1=iso8601,2=json_array,3=pe_hex12,4=deleted_at |
| parallel-r1 | 6 | 1494 | 181 | 635 | 5/5 | 10/10 | 93 | 130/4 | ❌ | 1=iso8601,2=json_array,3=pe_hex12,4=deleted_at |

## Notes


