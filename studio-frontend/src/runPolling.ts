export function isTerminalPollStatus(status: number): boolean {
  return status === 401 || status === 403 || status === 404;
}

export function nextPollDelay(consecutiveErrors: number): number {
  // exponentially from 1.5s to 15s
  const base = 1500;
  const max = 15000;
  if (consecutiveErrors <= 1) return base;
  const delay = base * Math.pow(1.5, consecutiveErrors - 1);
  return Math.min(Math.round(delay), max);
}

export function shouldStopPollingAfterErrors(consecutiveErrors: number): boolean {
  return consecutiveErrors >= 10;
}
