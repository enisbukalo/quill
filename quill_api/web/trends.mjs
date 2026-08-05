export function linearTrend(values) {
  const series = (values || []).map((value) => Number(value));
  if (!series.length || series.some((value) => !Number.isFinite(value))) return [];
  if (series.length === 1) return [...series];

  const midpoint = (series.length - 1) / 2;
  const mean = series.reduce((total, value) => total + value, 0) / series.length;
  let numerator = 0;
  let denominator = 0;
  series.forEach((value, index) => {
    const offset = index - midpoint;
    numerator += offset * (value - mean);
    denominator += offset * offset;
  });
  const slope = denominator ? numerator / denominator : 0;
  return series.map((_, index) => Math.max(0, mean + slope * (index - midpoint)));
}

export function sparklineLeftMargin(labels) {
  const longest = Math.max(0, ...(labels || []).map((label) => String(label).length));
  return Math.max(28, Math.min(96, longest * 6 + 12));
}
