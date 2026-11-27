import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react';
import { Api, ApiError } from './client';

const TOKEN_STORAGE = 'por-api-token';

function savedToken() {
  try {
    return sessionStorage.getItem(TOKEN_STORAGE) ?? '';
  } catch {
    return '';
  }
}

const AuthContext = createContext<{
  api: Api;
  authenticated: boolean;
  saveToken: (token: string) => void;
} | null>(null);

export function ApiProvider({ children }: { children: ReactNode }) {
  const [access, setAccess] = useState(() => ({
    token: savedToken(),
    revision: 0,
    client: makeQueryClient(),
  }));
  const api = useMemo(() => new Api(access.token), [access.token]);

  function saveToken(value: string) {
    const token = value.trim();
    try {
      if (token) sessionStorage.setItem(TOKEN_STORAGE, token);
      else sessionStorage.removeItem(TOKEN_STORAGE);
    } catch {
      // Access can still work in this page if the browser disables storage.
    }
    setAccess((previous) => ({
      token,
      revision: previous.revision + 1,
      client: makeQueryClient(),
    }));
  }

  return (
    <AuthContext.Provider value={{ api, authenticated: Boolean(access.token), saveToken }}>
      <QueryClientProvider key={access.revision} client={access.client}>
        {children}
      </QueryClientProvider>
    </AuthContext.Provider>
  );
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 5000,
        retry: (count, error) => count < 1 && (!(error instanceof ApiError) || error.status >= 500),
      },
      mutations: { retry: false },
    },
  });
}

export function useApi() {
  const value = useContext(AuthContext);
  if (!value) throw new Error('ApiProvider is required');
  return value;
}
