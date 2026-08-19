import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const clientSource = readFileSync(
  new URL('../src/shared/api/client.ts', import.meta.url),
  'utf8',
);

const demoUserBlock = clientSource.match(
  /export const DEMO_USER:[\s\S]*?=\s*\{([\s\S]*?)\n\};/,
)?.[1] ?? '';

test('demo identity is an internal all-employee, not a wildcard or privileged user', () => {
  assert.match(demoUserBlock, /principals:\s*\['group:all-employees'\]/);
  assert.doesNotMatch(demoUserBlock, /principals:\s*\['\*'\]/);
  assert.doesNotMatch(demoUserBlock, /group:people-managers|group:security-operations/);
  assert.match(demoUserBlock, /classification:\s*'internal'/);
});
