export interface LabeledSource {
  label: string;
}

export interface CitationProjection<T extends LabeledSource> {
  content: string;
  sources: Array<{ source: T; displayIndex: number }>;
}

const SOURCE_GROUP = /\[S(\d+(?:\s*,\s*S?\d+)*)\]/gi;

/**
 * Project backend context labels (which may contain gaps such as S1, S2, S4)
 * into the contiguous, first-citation order shown by the UI.
 *
 * Backend labels remain useful inside the RAG trace, but exposing them directly
 * made the source row look as if a card had disappeared. The UI contract is
 * intentionally simpler: if three sources are cited, they are citations 1–3.
 */
export function projectCitations<T extends LabeledSource>(
  answer: string,
  availableSources: T[],
): CitationProjection<T> {
  const available = new Map(
    availableSources.map((source) => [source.label.toUpperCase(), source]),
  );
  const labels: string[] = [];
  const seen = new Set<string>();

  for (const match of answer.matchAll(SOURCE_GROUP)) {
    for (const value of match[1].split(',')) {
      const number = value.replace(/[^\d]/g, '');
      const label = `S${number}`;
      if (number && available.has(label) && !seen.has(label)) {
        seen.add(label);
        labels.push(label);
      }
    }
  }

  const displayByLabel = new Map(labels.map((label, index) => [label, index + 1]));
  const renumberedText = answer.replace(
    /\b(Source|Nguồn)\s+(\d+)(?=\s+\[S\2\])/gi,
    (original, word: string, number: string) => {
      const displayIndex = displayByLabel.get(`S${number}`);
      return displayIndex == null ? original : `${word} ${displayIndex}`;
    },
  );
  const content = renumberedText.replace(SOURCE_GROUP, (original, inner: string) => {
    const projected = inner
      .split(',')
      .map((value) => {
        const number = value.replace(/[^\d]/g, '');
        const displayIndex = displayByLabel.get(`S${number}`);
        return displayIndex == null ? '' : `[[cite:${displayIndex}]]`;
      })
      .join('');
    return projected || original;
  });

  return {
    content,
    sources: labels.map((label, index) => ({
      source: available.get(label) as T,
      displayIndex: index + 1,
    })),
  };
}
