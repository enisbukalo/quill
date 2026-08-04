/** Enable exact per-request usage on vLLM's OpenAI-compatible streaming responses. */
export default function vllmLiveUsage(pi) {
  pi.on("before_provider_request", (event) => {
    const payload = event.payload;
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || payload.stream !== true) {
      return undefined;
    }
    const current = payload.stream_options;
    const streamOptions = current && typeof current === "object" && !Array.isArray(current) ? current : {};
    return {
      ...payload,
      stream_options: {
        ...streamOptions,
        include_usage: true,
        continuous_usage_stats: true,
      },
    };
  });
}
