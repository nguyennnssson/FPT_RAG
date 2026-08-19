import type { Conversation } from '../../../../types';

export interface ConversationGroup {
  label: string;
  items: Conversation[];
}

const ONE_DAY = 24 * 60 * 60 * 1000;

/** Group conversations into Today / Yesterday / Previous 7 days / Older. */
export function groupByDate(conversations: Conversation[], now = Date.now()): ConversationGroup[] {
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const todayStart = startOfToday.getTime();

  const buckets: Record<string, Conversation[]> = {
    Today: [],
    Yesterday: [],
    'Previous 7 days': [],
    Older: [],
  };

  const sorted = [...conversations].sort(
    (a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
  );

  for (const c of sorted) {
    const t = new Date(c.updatedAt).getTime();
    if (t >= todayStart) buckets.Today.push(c);
    else if (t >= todayStart - ONE_DAY) buckets.Yesterday.push(c);
    else if (t >= todayStart - 7 * ONE_DAY) buckets['Previous 7 days'].push(c);
    else buckets.Older.push(c);
  }

  return Object.entries(buckets)
    .filter(([, items]) => items.length > 0)
    .map(([label, items]) => ({ label, items }));
}
