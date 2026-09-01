/** Fail-closed numeric URL parameters for the public and offline render paths. */

export function numericParam(query, name, fallback, { integer = false, min = -Infinity, max = Infinity } = {}) {
  const raw = query.get(name);
  if (raw === null || raw.trim() === "") return fallback;
  const value = Number(raw);
  const valid = Number.isFinite(value) && (!integer || Number.isInteger(value)) && value >= min && value <= max;
  if (!valid) throw new Error(`invalid ${name}: ${raw}`);
  return value;
}
