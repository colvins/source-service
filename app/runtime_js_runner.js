const path = require("path");

async function main() {
  const scriptPath = process.argv[2];
  const action = process.env.COLVINS_ACTION;
  const params = JSON.parse(process.env.COLVINS_PARAMS || "{}");
  const context = JSON.parse(process.env.COLVINS_CONTEXT || "{}");

  if (!scriptPath || !action) {
    throw new Error("missing runtime arguments");
  }

  const mod = require(path.resolve(scriptPath));
  const fn = mod && mod[action];
  if (typeof fn !== "function") {
    throw new Error(`source does not export function: ${action}`);
  }

  const result = await fn(params, context);
  process.stdout.write(JSON.stringify(result || {}));
}

main().catch((error) => {
  process.stderr.write((error && error.stack) || String(error));
  process.exit(1);
});
