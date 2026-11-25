import type { ResearchRequest, Schema } from '@/api/client';
import { newKey } from '@/lib/presentation';

export type SelectedImage = {
  id: string;
  file: File;
  mediaType: Schema['QueryImage']['media_type'];
};

export type PendingSubmission = {
  key: string;
  request: Omit<ResearchRequest, 'images'>;
  files: SelectedImage[];
};

const TAB_KEY = 'por-pending-submission-tab';

function tabId() {
  const existing = sessionStorage.getItem(TAB_KEY);
  if (existing) return existing;
  const id = newKey();
  sessionStorage.setItem(TAB_KEY, id);
  return id;
}

// Store the exact pending body, including Files, until admission is confirmed.
// A refresh can check the receipt or explicitly resend the same key.
async function withDraft<T>(
  mode: IDBTransactionMode,
  operation: (store: IDBObjectStore, id: string) => IDBRequest<T>,
) {
  const id = tabId();
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open('por-submissions', 1);
    req.onupgradeneeded = () => req.result.createObjectStore('pending');
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = db.transaction('pending', mode);
      const request = operation(transaction.objectStore('pending'), id);
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  } finally {
    db.close();
  }
}

export function readSubmission() {
  return withDraft<PendingSubmission | undefined>('readonly', (store, id) => store.get(id));
}

export function saveSubmission(draft: PendingSubmission) {
  return withDraft('readwrite', (store, id) => store.put(draft, id));
}

export function clearSubmission() {
  return withDraft('readwrite', (store, id) => store.delete(id));
}

function encodeImage({ file, mediaType }: SelectedImage) {
  return new Promise<Schema['QueryImage']>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('无法读取图片 ' + file.name));
    reader.onload = () => {
      const value = String(reader.result);
      resolve({
        name: file.name,
        media_type: mediaType,
        data: value.slice(value.indexOf(',') + 1),
      });
    };
    reader.readAsDataURL(file);
  });
}

export async function submissionBody(draft: PendingSubmission): Promise<ResearchRequest> {
  return { ...draft.request, images: await Promise.all(draft.files.map(encodeImage)) };
}
