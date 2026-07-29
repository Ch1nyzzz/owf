import { test } from "node:test";
import assert from "node:assert/strict";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { StreamFn } from "@earendil-works/pi-agent-core";
import { withTransportRetry } from "../src/retry.js";

const journalStub = { write: () => {} } as unknown as import("../src/journal.js").Journal;

function streamOf(message: object): ReturnType<typeof createAssistantMessageEventStream> {
	const s = createAssistantMessageEventStream();
	s.end(message as never);
	return s;
}

const errorMsg = { role: "assistant", content: [], stopReason: "error", errorMessage: "400 status code (no body)" };
const okMsg = { role: "assistant", content: [{ type: "text", text: "answer" }], stopReason: "stop" };
const partialErrorMsg = { role: "assistant", content: [{ type: "text", text: "partial" }], stopReason: "error", errorMessage: "boom" };

const NO_BACKOFF = [0];

test("transient error retries until success", async () => {
	let calls = 0;
	const fn: StreamFn = () => streamOf(++calls < 3 ? errorMsg : okMsg);
	const stream = await withTransportRetry(fn, journalStub, NO_BACKOFF)({ id: "m" } as never, {} as never);
	for await (const _ of stream) { /* drain */ }
	const result = (await stream.result()) as typeof okMsg;
	assert.equal(calls, 3);
	assert.equal(result.stopReason, "stop");
});

test("persistent error gives up after max attempts", async () => {
	let calls = 0;
	const fn: StreamFn = () => (calls++, streamOf(errorMsg));
	const stream = await withTransportRetry(fn, journalStub, NO_BACKOFF)({ id: "m" } as never, {} as never);
	for await (const _ of stream) { /* drain */ }
	const result = (await stream.result()) as unknown as typeof errorMsg;
	assert.equal(calls, 4);
	assert.equal(result.stopReason, "error");
});

test("error after streamed text is forwarded, not retried", async () => {
	let calls = 0;
	const fn: StreamFn = () => (calls++, streamOf(partialErrorMsg));
	const stream = await withTransportRetry(fn, journalStub, NO_BACKOFF)({ id: "m" } as never, {} as never);
	for await (const _ of stream) { /* drain */ }
	const result = (await stream.result()) as typeof partialErrorMsg;
	assert.equal(calls, 1);
	assert.equal(result.errorMessage, "boom");
});
