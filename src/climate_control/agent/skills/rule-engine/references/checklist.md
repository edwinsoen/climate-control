# Submission checklist

- [ ] Every `device_id` exists in context, and every metric is in that device's
      `allowed_actions.metrics` (or capabilities for other devices).
- [ ] Every `on` action uses a fan setting from the target's
      `allowed_actions.fan`.
- [ ] Indoor thresholds use µg/m³ (or rank 1–4 for `air_quality`); outdoor
      thresholds use AQI. No outdoor value is compared to an indoor threshold.
- [ ] Rules are ordered most specific first; high thresholds precede low ones.
- [ ] Outdoor or other-device branches are followed by indoor-only or explicit
      `unavailable` branches.
- [ ] The fallback is intentional (`hold` is usual for hysteresis).
- [ ] Time windows cover the intended days, including overnight spans, in the
      ruleset `timezone`.
- [ ] Existing household policy (quiet hours, allowed settings) is preserved
      unless the reason explains the change.
- [ ] Rule IDs and trigger IDs are unique; explanations are short and plain.
- [ ] `preview_rules` returns no errors.
- [ ] The submission `reason` states what changed and the evidence for it, or
      that the current rules remain appropriate.
