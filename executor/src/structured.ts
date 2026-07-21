import type { AgentTool } from "@earendil-works/pi-agent-core";

export const SUBMIT_TOOL_NAME = "submit_result";

export const SUBMIT_INSTRUCTION = `\nWhen you have the final answer, call the ${SUBMIT_TOOL_NAME} tool exactly once with the answer in the required format. Do not describe the answer in prose instead of calling the tool.`;

/**
 * Structured output (DSL §5): a forced tool whose parameters are the workflow-provided
 * JSON Schema. The agent loop validates arguments via typebox; a successful call captures
 * the object and terminates the node.
 */
export function makeSubmitTool(schema: object, onSubmit: (value: object) => void): AgentTool<any> {
	return {
		name: SUBMIT_TOOL_NAME,
		label: "Submit result",
		description: "Submit the final structured answer. Ends the task.",
		parameters: schema as any,
		execute: async (_id, params) => {
			onSubmit(params as object);
			return {
				content: [{ type: "text", text: "Result accepted." }],
				details: {},
				terminate: true,
			};
		},
	};
}
