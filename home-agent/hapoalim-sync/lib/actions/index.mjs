import { register, get, list, prepare, runAll, _resetForTests } from "./registry.mjs";
import checkingTransactions from "./checking_transactions.mjs";

register(checkingTransactions);
// Future: register(balances) — see _example_balances.mjs (stub, not registered).

export { register, get, list, prepare, runAll, _resetForTests };
