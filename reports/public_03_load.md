# Grade report — public_03_load

## Outcomes vs baseline (no-op controller)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.356 | +4.3% | 0.244 | +0.1% | 0.519 | +3.7% | 24% | 416 (408) |
| arav_kyc_1 | 0.357 | +0.8% | 0.216 | +0.5% | 0.595 | -0.1% | 0% | 345 (370) |
| arav_renewal_0 | 0.429 | +6.7% | 0.262 | -0.8% | 0.584 | +7.5% | 35% | 435 (440) |
| kest_cancel_2 | 0.356 | +5.0% | 0.245 | +0.5% | 0.517 | +3.7% | 24% | 383 (411) |
| kest_kyc_1 | 0.355 | +0.6% | 0.214 | +0.2% | 0.594 | -0.1% | 0% | 346 (339) |
| kest_renewal_0 | 0.440 | +10.1% | 0.264 | -0.0% | 0.593 | +9.3% | 45% | 422 (433) |
| nort_cancel_2 | 0.368 | +8.6% | 0.245 | +0.9% | 0.531 | +6.5% | 44% | 387 (393) |
| nort_kyc_1 | 0.365 | +3.0% | 0.214 | +0.2% | 0.609 | +2.3% | 12% | 340 (323) |
| nort_renewal_0 | 0.444 | +9.8% | 0.263 | -0.1% | 0.598 | +9.2% | 49% | 453 (411) |
| tide_cancel_2 | 0.347 | +2.5% | 0.244 | -0.2% | 0.510 | +2.1% | 17% | 416 (407) |
| tide_kyc_1 | 0.366 | +3.5% | 0.215 | +0.1% | 0.610 | +3.1% | 12% | 353 (356) |
| tide_renewal_0 | 0.404 | +0.9% | 0.263 | -0.4% | 0.548 | +1.0% | 0% | 395 (438) |
| ALL | 0.382 | +4.7% | 0.241 | +0.1% | 0.566 | +4.1% | 22% | 4691 (4729) |

## Changes applied by the controller

48 applied — neutral: 24, beneficial: 24

- arav_cancel_2 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_renewal_0 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_cancel_2 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_kyc_1 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- kest_renewal_0 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_cancel_2 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_kyc_1 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_renewal_0 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_cancel_2 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_kyc_1 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- tide_renewal_0 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- nort_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- arav_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- arav_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.068 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- nort_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.068 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=-0.002 didΔ=+0.000 → **neutral**
- kest_renewal_0 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.068 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- arav_kyc_1 v2 (controller_approved) dims=['cohorts'] engageΔ=+0.000 didΔ=+0.000 → **neutral**
- arav_cancel_2 v4 (controller_approved) dims=['voice'] engageΔ=+0.039 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v4 (controller_approved) dims=['voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.048 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v4 (controller_approved) dims=['voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v4 (controller_approved) dims=['welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- tide_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.014 didΔ=+0.000 → **beneficial**
- nort_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.045 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v3 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.048 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v5 (controller_approved) dims=['voice'] engageΔ=+0.040 didΔ=+0.000 → **beneficial**
- arav_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v6 (controller_approved) dims=['voice'] engageΔ=+0.024 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.047 didΔ=+0.000 → **beneficial**
- kest_cancel_2 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.019 didΔ=+0.000 → **beneficial**
- nort_kyc_1 v4 (controller_approved) dims=['welcome_message', 'voice'] engageΔ=+0.011 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v4 (controller_approved) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- arav_cancel_2 v5 (controller_approved) dims=['welcome_message'] engageΔ=+0.012 didΔ=+0.000 → **beneficial**
- nort_cancel_2 v7 (controller_approved) dims=['welcome_message'] engageΔ=+0.013 didΔ=+0.000 → **beneficial**
- kest_renewal_0 v6 (controller_approved) dims=['voice'] engageΔ=+0.020 didΔ=+0.000 → **beneficial**
- tide_kyc_1 v5 (controller_approved) dims=['voice'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- nort_cancel_2 v8 (controller_approved) dims=['welcome_message'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- tide_cancel_2 v6 (controller_approved) dims=['welcome_message'] engageΔ=+0.008 didΔ=+0.000 → **neutral**
- tide_kyc_1 v6 (controller_approved) dims=['voice'] engageΔ=+0.005 didΔ=+0.000 → **neutral**
- nort_kyc_1 v5 (controller_approved) dims=['voice'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- tide_kyc_1 v7 (controller_approved) dims=['voice'] engageΔ=-0.005 didΔ=+0.000 → **neutral**
- nort_cancel_2 v9 (controller_approved) dims=['voice'] engageΔ=+0.000 didΔ=+0.000 → **neutral**

## Governance

```
{
  "approval_requests": 55,
  "decisions": {
    "approved": 48,
    "rejected": 4,
    "expired": 2
  },
  "rejection_reasons": {
    "approver declined (judgement call)": 4
  },
  "applied_via_approval": 48,
  "auto_applied": 0,
  "auto_apply_rejected": 0,
  "apply_conflicts": 0,
  "lapsed": 0,
  "peak_requests_per_24h": {
    "arav_cancel_2": 3,
    "arav_kyc_1": 2,
    "arav_renewal_0": 2,
    "kest_cancel_2": 3,
    "kest_kyc_1": 1,
    "kest_renewal_0": 2,
    "nort_cancel_2": 3,
    "nort_kyc_1": 2,
    "nort_renewal_0": 2,
    "tide_cancel_2": 3,
    "tide_kyc_1": 2,
    "tide_renewal_0": 2
  }
}
```

## Incidents

```
[]
```

## Decaying variants — share of in-population calls still on the variant, by day after onset

```
{
  "nort_renewal_0:welcome_message=W3": {
    "3": 0.725,
    "4": 0.729
  },
  "kest_renewal_0:welcome_message=W3": {
    "3": 0.818,
    "4": 0.82
  },
  "arav_renewal_0:welcome_message=W3": {
    "3": 0.585,
    "4": 0.593
  }
}
```

Credits used: 0 · validation runs: 0 · LLM fix calls: 0