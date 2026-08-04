export function reconcileModelOverrides(current, phases, models, preserve) {
  if (!preserve || !current.enabled) {
    return { enabled: false, overrides: {} };
  }
  const available = new Set(models);
  return {
    enabled: true,
    overrides: Object.fromEntries(
      phases.map((phase) => [
        phase.id,
        available.has(current.overrides[phase.id]) ? current.overrides[phase.id] : phase.model,
      ]),
    ),
  };
}
