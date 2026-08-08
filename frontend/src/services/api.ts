import axios from 'axios';
import { WorkbookSchema, WorksheetSchema, CellSchema } from 'backend/app/schema/workbook_schema';
import type { AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { getApp, getApps, initializeApp } from 'firebase/app';
import type { FirebaseApp, FirebaseOptions } from 'firebase/app';
import { getAuth } from 'firebase/auth';
import type { Auth } from 'firebase/auth';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL;

// Firebase web configuration, injected at build time by react-scripts. Every value here is
// public by design and none of them is a secret. The project configured here must be the
// project the backend accepts tokens from, which it derives from PROJECT_ID.
// The keys are documented in .env.example.
const FIREBASE_CONFIG: Record<string, string | undefined> = {
  apiKey: process.env.REACT_APP_FIREBASE_API_KEY,
  authDomain: process.env.REACT_APP_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.REACT_APP_FIREBASE_PROJECT_ID,
  appId: process.env.REACT_APP_FIREBASE_APP_ID,
};

// Optional fields: the Auth SDK does not need them, so they are passed through without
// being required.
const FIREBASE_OPTIONAL_CONFIG: Record<string, string | undefined> = {
  storageBucket: process.env.REACT_APP_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.REACT_APP_FIREBASE_MESSAGING_SENDER_ID,
};

// Header the bearer token travels in. A header bag may hold it under any casing, so removal
// matches case-insensitively.
const AUTHORIZATION_HEADER = 'Authorization';
const AUTHORIZATION_HEADER_PATTERN = /^authorization$/i;

const NOT_AUTHENTICATED = 'Not authenticated: no Firebase ID token is available for this request.';

// SECURITY: cell writes are coalesced into one request per worksheet — one request per edited
// cell let an ordinary paste, fill or autosave burst spend the whole per-client write budget
// and be refused with 429.
//
// The window bounds this client's own write rate: at most one PUT per worksheet per window,
// so 500 ms is at most 120 requests a minute per edited worksheet. The backend
// `rate_limit_write` budget is set to twice that, which leaves room for the other write
// routes and for a second worksheet being edited at the same time.
const CELL_WRITE_COALESCE_MS = 500;

interface CellWriteWaiter {
  cell: CellSchema;
  resolve: (cell: CellSchema) => void;
  reject: (reason: unknown) => void;
}

interface PendingCellWrites {
  waiters: CellWriteWaiter[];
  // The DOM and Node typings disagree on what setTimeout returns, so the handle type is
  // derived from the function rather than named.
  timer: ReturnType<typeof setTimeout> | null;
}

// One batch per (workbook, worksheet). Writes to different worksheets are never merged,
// because the route addresses one worksheet.
const pendingCellWrites = new Map<string, PendingCellWrites>();

// webpack defines this on every module of a development build; a production bundle has no `hot`.
declare const module: { hot?: { dispose?: (callback: () => void) => void } } | undefined;

/**
 * Return the Firebase web configuration, or throw naming what is missing.
 *
 * @throws Error if any required field is absent from the build environment.
 */
function firebaseOptions(): FirebaseOptions {
  const missing = Object.keys(FIREBASE_CONFIG).filter((key) => !FIREBASE_CONFIG[key]);
  if (missing.length > 0) {
    throw new Error(
      `Firebase web configuration is incomplete: set REACT_APP_FIREBASE_${missing
        .map((key) => key.replace(/([A-Z])/g, '_$1').toUpperCase())
        .join(', REACT_APP_FIREBASE_')} in the frontend build environment.`
    );
  }
  return { ...FIREBASE_CONFIG, ...FIREBASE_OPTIONAL_CONFIG } as FirebaseOptions;
}

/**
 * Return the default Firebase app, initialising it on first use.
 *
 * SECURITY: the SDK is initialised here — no module initialised it, so `getAuth()`
 * resolved no app and every request went out with no credential.
 *
 * The app initialised is the *default* one, which `getAuth()` and `getFirestore()` resolve
 * when called with no argument.
 */
function firebaseApp(): FirebaseApp {
  return getApps().length > 0 ? getApp() : initializeApp(firebaseOptions());
}

// SECURITY: the default app is initialised as this module is evaluated, before the sign-in
// path's argument-less `getAuth()` runs — initialising it on the first API request instead
// left that call resolving no app, so signing in failed before a credential could exist.
// A configuration failure is reported here and reaches API callers from the first request,
// because importing this module must not throw.
try {
  firebaseApp();
} catch (error) {
  console.error('Error initialising Firebase:', describeFailure(error));
}

// Resolved once and reused. A failed attempt is not memoised, so a later request retries.
let authenticationReady: Promise<Auth> | null = null;

/**
 * Resolve to the shared `Auth` instance once Firebase has restored any persisted session.
 *
 * A page reload restores the signed-in user asynchronously, so `currentUser` is null for
 * the first moments of the new document. Waiting for the initial auth state to settle is
 * what stops a request issued during that window from being sent without a credential.
 */
function resolvedAuthentication(): Promise<Auth> {
  if (authenticationReady === null) {
    authenticationReady = (async (): Promise<Auth> => {
      const auth = getAuth(firebaseApp());
      await auth.authStateReady();
      return auth;
    })().catch((error: unknown) => {
      authenticationReady = null;
      throw error;
    });
  }
  return authenticationReady;
}

/**
 * Remove the header from one header bag, whether it is an `AxiosHeaders` instance, which
 * deletes case-insensitively through its own method, or a plain object.
 */
function deleteAuthorizationHeader(headers: unknown): void {
  if (typeof headers !== 'object' || headers === null) {
    return;
  }
  const headerBag = headers as Record<string, unknown> & { delete?: (name: string) => unknown };
  if (typeof headerBag.delete === 'function') {
    headerBag.delete(AUTHORIZATION_HEADER);
  }
  Object.keys(headerBag)
    .filter((name) => AUTHORIZATION_HEADER_PATTERN.test(name))
    .forEach((name) => {
      delete headerBag[name];
    });
}

/**
 * SECURITY: remove the bearer token from a failed request — Axios keeps the request
 * configuration on the error, so the token reached logs and callers through the error object.
 */
function redactAuthorizationHeader<T>(error: T): T {
  const failure = error as
    | { config?: { headers?: unknown }; response?: { config?: { headers?: unknown } } }
    | null
    | undefined;
  deleteAuthorizationHeader(failure?.config?.headers);
  deleteAuthorizationHeader(failure?.response?.config?.headers);
  return error;
}

/**
 * Reduce a failure to the fields that are safe to log: no headers, no body, no token.
 */
function describeFailure(error: unknown): { status?: number; code?: string; message: string } {
  if (axios.isAxiosError(error)) {
    return { status: error.response?.status, code: error.code, message: error.message };
  }
  return { message: error instanceof Error ? error.message : 'Unknown error' };
}

// Requests go through this instance rather than the global `axios` default.
// SECURITY: the token-attaching interceptor is confined to API calls — installing it on
// the global default attached the credential to every axios call anywhere in the bundle.
const apiClient: AxiosInstance = axios.create({ baseURL: API_BASE_URL });

// SECURITY: attach the current Firebase ID token for server-side verification — requests
// previously carried no credential, so every authenticated route answered 401.
const authorizationRequestInterceptorId = apiClient.interceptors.request.use(
  async (config: InternalAxiosRequestConfig) => {
    const auth = await resolvedAuthentication();
    const token = await auth.currentUser?.getIdToken();
    if (!token) {
      // SECURITY: an API request is refused here rather than sent unauthenticated.
      throw new Error(NOT_AUTHENTICATED);
    }
    config.headers[AUTHORIZATION_HEADER] = `Bearer ${token}`;
    return config;
  }
);

const authorizationErrorInterceptorId = apiClient.interceptors.response.use(
  undefined,
  (error: unknown) => Promise.reject(redactAuthorizationHeader(error))
);

// Both registrations belong to this module instance, so a development reload discards them
// instead of leaving a further pair on the client.
const hotModuleApi = typeof module === 'undefined' ? undefined : module?.hot;
if (hotModuleApi?.dispose) {
  hotModuleApi.dispose(() => {
    apiClient.interceptors.request.eject(authorizationRequestInterceptorId);
    apiClient.interceptors.response.eject(authorizationErrorInterceptorId);
  });
}

export const fetchWorkbooks = async (): Promise<WorkbookSchema[]> => {
  try {
    const response = await apiClient.get('/workbooks');
    return response.data;
  } catch (error) {
    console.error('Error fetching workbooks:', describeFailure(error));
    throw error;
  }
};

export const createWorkbook = async (workbook: WorkbookSchema): Promise<WorkbookSchema> => {
  try {
    const response = await apiClient.post('/workbooks', workbook);
    return response.data;
  } catch (error) {
    console.error('Error creating workbook:', describeFailure(error));
    throw error;
  }
};

/**
 * Key identifying the batch a cell write belongs to. The separator is a character a path
 * segment cannot contain, so two different worksheets can never collide on one key.
 */
function cellWriteKey(workbookId: string, worksheetId: string): string {
  return `${workbookId}\u0000${worksheetId}`;
}

/**
 * Send every cell queued for one worksheet as a single request, then settle its callers.
 *
 * The batch is removed from the queue before the request is issued, so writes arriving while
 * it is in flight accumulate into the next batch instead of joining one already sent.
 */
async function flushCellWrites(workbookId: string, worksheetId: string): Promise<void> {
  const key = cellWriteKey(workbookId, worksheetId);
  const pending = pendingCellWrites.get(key);
  if (pending === undefined) {
    return;
  }
  pendingCellWrites.delete(key);
  if (pending.timer !== null) {
    clearTimeout(pending.timer);
  }
  const { waiters } = pending;
  try {
    // The route takes a list of cells, which is what makes one request per batch possible.
    // Its success response is an acknowledgement message rather than cells, so each caller
    // is settled with the cell it supplied.
    await apiClient.put(
      `/workbooks/${workbookId}/worksheets/${worksheetId}/cells`,
      waiters.map((waiter) => waiter.cell)
    );
    waiters.forEach((waiter) => waiter.resolve(waiter.cell));
  } catch (error) {
    console.error('Error updating cells:', describeFailure(error));
    waiters.forEach((waiter) => waiter.reject(error));
  }
}

// HUMAN ASSISTANCE NEEDED
// This function might need additional error handling or data validation
export const updateCell = (workbookId: string, worksheetId: string, cell: CellSchema): Promise<CellSchema> =>
  new Promise<CellSchema>((resolve, reject) => {
    const key = cellWriteKey(workbookId, worksheetId);
    let pending = pendingCellWrites.get(key);
    if (pending === undefined) {
      pending = { waiters: [], timer: null };
      pendingCellWrites.set(key, pending);
    }
    pending.waiters.push({ cell, resolve, reject });
    if (pending.timer === null) {
      pending.timer = setTimeout(() => {
        void flushCellWrites(workbookId, worksheetId);
      }, CELL_WRITE_COALESCE_MS);
    }
  });
