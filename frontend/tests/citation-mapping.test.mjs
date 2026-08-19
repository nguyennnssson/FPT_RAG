import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { projectCitations } from '../src/shared/api/citations.ts';

const sources = [
  { label: 'S1', title: 'Onboarding' },
  { label: 'S2', title: 'Handbook' },
  { label: 'S4', title: 'Security' },
];

test('backend citation gaps are projected to contiguous visible numbers', () => {
  const projected = projectCitations(
    'Primary [S1], security [S4], then handbook [S2].',
    sources,
  );

  assert.equal(
    projected.content,
    'Primary [[cite:1]], security [[cite:2]], then handbook [[cite:3]].',
  );
  assert.deepEqual(
    projected.sources.map(({ source, displayIndex }) => [source.label, displayIndex]),
    [['S1', 1], ['S4', 2], ['S2', 3]],
  );
});

test('grouped citations and citations inside Markdown emphasis are projected', () => {
  const projected = projectCitations(
    '**Source 3 [S3] appears before Source 4 [S4].** [S3, S4]',
    [{ label: 'S3' }, { label: 'S4' }],
  );

  assert.equal(
    projected.content,
    '**Source 1 [[cite:1]] appears before Source 2 [[cite:2]].** '
      + '[[cite:1]][[cite:2]]',
  );
});

test('Markdown renderer handles citation tokens inside strong text', () => {
  const renderer = readFileSync(
    new URL('../src/shared/ui/markdown/MarkdownRenderer/MarkdownRenderer.tsx', import.meta.url),
    'utf8',
  );
  assert.match(
    renderer,
    /strong:\s*\(\{ children \}\)\s*=>\s*<strong>\{withCitations\(children, onCitation\)\}<\/strong>/,
  );
});
