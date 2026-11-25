import { useState } from 'react';

export function usePages() {
  const [offsets, setOffsets] = useState([0]);
  const offset = offsets.at(-1)!;
  return {
    offset,
    label: '第 ' + offsets.length + ' 页',
    previous: offsets.length > 1 ? () => setOffsets((pages) => pages.slice(0, -1)) : undefined,
    next: (cursor: number) => {
      if (cursor > offset) setOffsets((pages) => [...pages, cursor]);
    },
    reset: () => setOffsets([0]),
  };
}
