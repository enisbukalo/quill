import assert from "node:assert/strict";
import test from "node:test";

import extension from "../../quill/pi_extensions/vllm_live_usage.mjs";

function handler() {
  let callback;
  extension({ on(name, value) { assert.equal(name, "before_provider_request"); callback = value; } });
  return callback;
}

test("Pi extension enables continuous usage without dropping payload options", () => {
  const rewrite = handler();
  const payload = { model: "Gemma", stream: true, temperature: 0.2, stream_options: { extra: 1 } };
  assert.deepEqual(rewrite({ payload }), {
    ...payload,
    stream_options: { extra: 1, include_usage: true, continuous_usage_stats: true },
  });
  assert.deepEqual(payload.stream_options, { extra: 1 });
});

test("Pi extension leaves non-streaming and malformed payloads alone", () => {
  const rewrite = handler();
  assert.equal(rewrite({ payload: { stream: false } }), undefined);
  assert.equal(rewrite({ payload: null }), undefined);
  assert.deepEqual(rewrite({ payload: { stream: true, stream_options: "bad" } }), {
    stream: true,
    stream_options: { include_usage: true, continuous_usage_stats: true },
  });
});
