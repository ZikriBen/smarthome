// Cal action registry: its own instance (the Hapoalim registry in ../../actions stays untouched in behavior).
import { createRegistry } from "../../actions/registry.mjs";
import cardTransactions from "./card_transactions.mjs";

const REG = createRegistry();
REG.register(cardTransactions);

export const { register, get, list, prepare, runAll, _resetForTests } = REG;
