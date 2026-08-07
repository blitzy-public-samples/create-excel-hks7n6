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

// HUMAN ASSISTANCE NEEDED
// This function might need additional error handling or data validation
export const updateCell = async (workbookId: string, worksheetId: string, cell: CellSchema): Promise<CellSchema> => {
  try {
    // The route takes a list of cells, so a single cell travels as a one-element list.
    // Its success response is an acknowledgement message rather than a cell, so the cell
    // that was accepted is returned here.
    await apiClient.put(
      `/workbooks/${workbookId}/worksheets/${worksheetId}/cells`,
      [cell]
    );
    return cell;
  } catch (error) {
    console.error('Error updating cell:', describeFailure(error));
    throw error;
  }
};
