/**
 * The translation service's tools, and nothing else, for the pi agent acting as its AI OS.
 *
 * Each tool runs `python -m pdf_translation.aios.tools <name>` with the arguments as JSON on
 * stdin, inside the job the service started this agent for (PDFTRANS_JOB_DIR). The Python side
 * enforces the budget and the quality gate; this file only forwards calls and trims output.
 */
import { spawnSync } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const PYTHON = process.env.PDFTRANS_PYTHON || "python";
const ROOT = process.env.PDFTRANS_ROOT || process.cwd();
const LIMIT = 40_000;

function call(name: string, params: unknown, signal?: AbortSignal) {
	const run = spawnSync(PYTHON, ["-m", "pdf_translation.aios.tools", name], {
		cwd: ROOT,
		input: JSON.stringify(params ?? {}),
		encoding: "utf-8",
		env: { ...process.env, PYTHONIOENCODING: "utf-8" },
		maxBuffer: 64 * 1024 * 1024,
		timeout: 60 * 60 * 1000,
	});
	if (signal?.aborted) throw new Error("aborted");
	const out = (run.stdout || "").trim();
	if (run.status !== 0) {
		throw new Error(`${name} failed: ${(out || run.stderr || "").slice(-4000)}`);
	}
	const text = out.length > LIMIT ? `${out.slice(0, LIMIT)}\n... (truncated; narrow the request)` : out;
	return { content: [{ type: "text" as const, text }], details: undefined };
}

const UnitIds = Type.Array(Type.String(), { description: "Unit ids, at most 40." });

export default function (pi: ExtensionAPI) {
	pi.registerTool({
		name: "problems",
		label: "Problems",
		description:
			"The job's current acceptance verdict: major problems (each blocks delivery), minor issue counts, and which repair actions the decision node already tried. Call this first.",
		parameters: Type.Object({}),
		async execute(_id, params, signal) {
			return call("problems", params, signal);
		},
	});
	pi.registerTool({
		name: "units",
		label: "Units",
		description:
			"Read passages (units): source text, current translation, state and warnings. Filter by unit_ids, pages (one-based, PDF only), states (accepted, needs_review, blocked, exhausted), with_issues, or category (a warning category such as number, untranslated_unit, retained_english).",
		parameters: Type.Object({
			unit_ids: Type.Optional(UnitIds),
			pages: Type.Optional(Type.Array(Type.Integer())),
			states: Type.Optional(Type.Array(Type.String())),
			with_issues: Type.Optional(Type.Boolean()),
			category: Type.Optional(Type.String()),
			limit: Type.Optional(Type.Integer({ description: "At most 50. Default 20." })),
		}),
		async execute(_id, params, signal) {
			return call("units", params, signal);
		},
	});
	pi.registerTool({
		name: "retranslate",
		label: "Retranslate",
		description:
			"Have the job's translation model translate units again, following your instruction (say what was wrong). Paid from the job's budget. Results that pass the quality gate are written; the answer lists what was applied and what was refused and why.",
		parameters: Type.Object({
			unit_ids: UnitIds,
			instruction: Type.String({ description: "What the new translation must do differently." }),
		}),
		async execute(_id, params, signal) {
			return call("retranslate", params, signal);
		},
	});
	pi.registerTool({
		name: "fix_units",
		label: "Fix units",
		description:
			"Write your own Chinese translation for units. Keep every ⟦P:...⟧ marker of the source exactly once; an opening ⟦P:aN⟧ must stay before its closing ⟦P:zN⟧. Each fix passes the quality gate or the whole batch is refused. Free.",
		parameters: Type.Object({
			fixes: Type.Array(
				Type.Object({
					unit_id: Type.String(),
					new_translated_text: Type.String(),
					reason: Type.String(),
				}),
			),
		}),
		async execute(_id, params, signal) {
			return call("fix_units", params, signal);
		},
	});
	pi.registerTool({
		name: "review_pages",
		label: "Review pages",
		description:
			"PDF jobs only: compare rendered pages with the original page images using the job's model and apply the fixes that pass the gate. At most 5 pages per call. Paid.",
		parameters: Type.Object({ pages: Type.Array(Type.Integer()) }),
		async execute(_id, params, signal) {
			return call("review_pages", params, signal);
		},
	});
	pi.registerTool({
		name: "render_and_check",
		label: "Render and check",
		description:
			"Write the outputs from the current translations and run acceptance again; returns the new verdict. Use it after a batch of fixes to see whether the problems are gone. Takes seconds for text formats and a few minutes for a PDF.",
		parameters: Type.Object({}),
		async execute(_id, params, signal) {
			return call("render_and_check", params, signal);
		},
	});
	pi.registerTool({
		name: "build_log",
		label: "Build log",
		description: "LaTeX jobs only: the end of the XeLaTeX log of the translated project, to find why it did not compile.",
		parameters: Type.Object({}),
		async execute(_id, params, signal) {
			return call("build_log", params, signal);
		},
	});
	pi.registerTool({
		name: "finish",
		label: "Finish",
		description:
			"End your work: summarise what you changed and what you could not fix (and why). The service then runs acceptance itself; you cannot declare the job deliverable.",
		parameters: Type.Object({ summary: Type.String() }),
		async execute(_id, params, signal) {
			return call("finish", params, signal);
		},
	});
}
