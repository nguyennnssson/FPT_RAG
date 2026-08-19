import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const composer = readFileSync(
  new URL('../src/features/chat/components/PromptComposer/PromptComposer.tsx', import.meta.url),
  'utf8',
);
const userMessage = readFileSync(
  new URL('../src/features/chat/components/UserMessage/UserMessage.tsx', import.meta.url),
  'utf8',
);
const client = readFileSync(
  new URL('../src/shared/api/client.ts', import.meta.url),
  'utf8',
);

test('sent image attachments render as images instead of upload announcement text', () => {
  assert.doesNotMatch(composer, /Tệp đính kèm đã được tải lên/);
  assert.match(composer, /previewUrl:\s*attachment\.isImage\s*\?\s*documentFileUrl/);
  assert.match(userMessage, /attachment\.kind === 'image'/);
  assert.match(userMessage, /<img[\s\S]*src=\{attachment\.previewUrl\}/);
});

test('attachment document ids are sent separately from visible question text', () => {
  assert.match(client, /attachment_doc_ids:\s*attachmentDocIds/);
  assert.match(client, /function mapStoredUserMessage/);
  assert.match(client, /previewUrl:\s*documentFileUrl\(docId\)/);
  assert.match(client, /Tệp đính kèm đã được tải lên/);
  assert.match(client, /forEach\(addAttachment\)/);
  assert.match(client, /rawDocId\.replace\(\/\\\.\+\$\//);
});
