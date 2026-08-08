import axios from 'axios';
import type { WorkbookSchema, CellSchema } from '../schema/workbookTypes';
import type { AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { getApp, getApps, initializeApp } from 'firebase/app';
import type { FirebaseApp, FirebaseOptions } from 'firebase/app';
import { getAuth } from 'firebase/auth';
import type { Auth } from 'firebase/auth';

const API_BASE_URL = (process.env.REACT_APP_API_BASE_URL ?? '').trim();

// CONTRACT: the API base URL must be an absolute http(s) URL naming an origin that is NOT the
// origin serving this document. Neither static edge routes API paths to the FastAPI service:
// the container serves files with an SPA fallback and the load balancer's URL map has one
// backend, the static bucket, which rewrites an unmatched path to /index.html. A relative or
// same-origin base URL therefore reaches static content, and a 200 carrying index.html would
// be returned to callers as workbook data.
const ABSOLUTE_HTTP_URL = /^https?:\/\/[^/?#]+/i;

const API_BASE_URL_NOT_CONFIGURED =
  'REACT_APP_API_BASE_URL must be set to the absolute URL of the API, for example ' +
  'https://api.example.com. It is unset or is not an absolute http(s) URL, so API requests ' +
  'would be sent to the origin serving this application, which serves no API.';

const API_BASE_URL_IS_THIS_ORIGIN =
  'REACT_APP_API_BASE_URL names the origin serving this application, which serves static ' +
  'files only and routes no API path to the API service. Set it to the API\'s own origin.';

// Firebase web configuration, injected at build time by react-scripts. Every value here is
// public by design and none of them is a secret. `projectId` must name the project the
// backend accepts tokens from, which is its `firebase_project_id` setting when that is set
// and its `PROJECT_ID` setting otherwise.
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

// User-facing messages for the three refusals the API makes deliberately. Without them every
// one of these arrived at a page as an indistinguishable generic failure, so a caller whose
// credential had simply expired - which `check_revoked=True` makes an expected outcome rather
// than an edge case, since signing out, a password reset or a disabled account all produce it -
// was shown "failed to load" with no indication that signing in again is the remedy.
const SESSION_EXPIRED =
  'Your session has ended. Sign in again to continue.';
const NOT_PERMITTED =
  'Your account is not permitted to do that.';
const RATE_LIMITED = 'Too many requests. Try again in a moment.';
const RATE_LIMITED_AFTER = (seconds: number): string =>
  `Too many requests. Try again in ${seconds} second${seconds === 1 ? '' : 's'}.`;
// The API answers 503 when it cannot complete a verification at all, which it separates from
// 401 deliberately: the credential was not rejected, so signing in again does not help.
const SERVICE_UNAVAILABLE =
  'The service is temporarily unavailable. Try again shortly.';

const UNAUTHORIZED_STATUS = 401;
const FORBIDDEN_STATUS = 403;
const TOO_MANY_REQUESTS_STATUS = 429;
const SERVICE_UNAVAILABLE_STATUS = 503;

/**
 * A failure carrying a message that is safe and useful to show a user, and whether the caller's
 * credential is the reason. `reauthenticate` is what a page keys a sign-in prompt off, so it
 * does not have to know which status codes mean that.
 */
export interface ApiFailure {
  status?: number;
  userMessage: string;
  reauthenticate: boolean;
  retryAfterSeconds?: number;
}

const API_FAILURE = '__apiFailure';

/** Whole seconds from a `Retry-After` header value, or undefined when it carries none. */
function retryAfterSeconds(headers: unknown): number | undefined {
  if (typeof headers !== 'object' || headers === null) {
    return undefined;
  }
  const bag = headers as Record<string, unknown> & { get?: (name: string) => unknown };
  const name = Object.keys(bag).find((key) => key.toLowerCase() === 'retry-after');
  const raw =
    name === undefined && typeof bag.get === 'function'
      ? bag.get('retry-after')
      : name === undefined
        ? undefined
        : bag[name];
  const seconds = Number(typeof raw === 'string' ? raw.trim() : raw);
  return Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : undefined;
}

/**
 * Attach an {@link ApiFailure} to `error`, classified from the response status.
 *
 * SECURITY: the classification is derived from the status code alone. No response body, header
 * or exception text reaches the returned message, so a server-side detail the API did not intend
 * to publish cannot travel to the interface through this path.
 */
function classifyFailure<T>(error: T): T {
  if (typeof error !== 'object' || error === null) {
    return error;
  }
  const failure = error as { response?: { status?: number; headers?: unknown } } & Record<
    string,
    unknown
  >;
  const status = failure.response?.status;
  let classified: ApiFailure;
  if (status === UNAUTHORIZED_STATUS) {
    classified = { status, userMessage: SESSION_EXPIRED, reauthenticate: true };
  } else if (status === FORBIDDEN_STATUS) {
    classified = { status, userMessage: NOT_PERMITTED, reauthenticate: false };
  } else if (status === TOO_MANY_REQUESTS_STATUS) {
    const seconds = retryAfterSeconds(failure.response?.headers);
    classified = {
      status,
      userMessage: seconds === undefined ? RATE_LIMITED : RATE_LIMITED_AFTER(seconds),
      reauthenticate: false,
      retryAfterSeconds: seconds,
    };
  } else if (status === SERVICE_UNAVAILABLE_STATUS) {
    classified = { status, userMessage: SERVICE_UNAVAILABLE, reauthenticate: false };
  } else {
    return error;
  }
  failure[API_FAILURE] = classified;
  return error;
}

/**
 * Return the classification the response interceptor attached to `error`, or undefined.
 *
 * A page calls this to tell a credential problem, a throttle and an outage apart from an
 * ordinary failure, and falls back to its own message when it returns undefined.
 */
export function apiFailure(error: unknown): ApiFailure | undefined {
  if (typeof error !== 'object' || error === null) {
    return undefined;
  }
  const carried = (error as Record<string, unknown>)[API_FAILURE];
  return carried === undefined ? undefined : (carried as ApiFailure);
}

/**
 * Return the message to show a user for `error`, or `fallback` when the API made no deliberate
 * refusal this client can explain.
 */
export function apiFailureMessage(error: unknown, fallback: string): string {
  return apiFailure(error)?.userMessage ?? fallback;
}

// SECURITY: cell writes are coalesced into one request per worksheet per window, so a paste,
// fill or autosave burst costs one request rather than one per edited cell.
//
// The window bounds this client's own write rate: at most one PUT per worksheet per window,
// so 1000 ms is at most 60 requests a minute per worksheet being edited. The backend
// `rate_limit_write` budget is 300 a minute, so continuous editing in five worksheets at once
// reaches it; four leaves 60 a minute for workbook creation and sharing. Both numbers are
// configuration on their side, and neither may be changed without the other: shortening this
// window or lowering that budget brings the two together and ordinary editing starts drawing
// 429s.
const CELL_WRITE_COALESCE_MS = 1000;

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
 * SECURITY: this is the only place the SDK is initialised, and the app it initialises is the
 * *default* one, which `getAuth()` and `getFirestore()` resolve when called with no argument.
 * Without it those calls resolve no app and no credential can be obtained.
 */
function firebaseApp(): FirebaseApp {
  return getApps().length > 0 ? getApp() : initializeApp(firebaseOptions());
}

/** Return the origin of `url` — scheme://host[:port] — lower-cased, or the empty string. */
function originOf(url: string): string {
  const match = ABSOLUTE_HTTP_URL.exec(url);
  return match === null ? '' : match[0].toLowerCase();
}

/**
 * Throw unless the configured API base URL is absolute and names another origin.
 *
 * SECURITY: an unconfigured or same-origin base URL is refused — API paths resolved against
 * the origin serving this application reach static content, so a 200 carrying the SPA
 * document was returned to callers as if it were API data.
 */
function assertApiBaseUrlIsAnApiOrigin(): void {
  if (!ABSOLUTE_HTTP_URL.test(API_BASE_URL)) {
    throw new Error(API_BASE_URL_NOT_CONFIGURED);
  }
  const documentOrigin =
    typeof window === 'undefined' ? '' : (window.location?.origin ?? '');
  if (documentOrigin !== '' && originOf(API_BASE_URL) === documentOrigin.toLowerCase()) {
    throw new Error(API_BASE_URL_IS_THIS_ORIGIN);
  }
}

// SECURITY: the default app is initialised as this module is evaluated, before the sign-in
// path's argument-less `getAuth()` runs — that call resolved no app, so signing in failed
// before a credential could exist.
// CONTRACT: importing this module must not throw, so a configuration failure is reported here
// and refused again per request.
try {
  firebaseApp();
} catch (error) {
  console.error('Error initialising Firebase:', describeFailure(error));
}

// Reported as this module is evaluated for the same reason, and enforced per request below.
try {
  assertApiBaseUrlIsAnApiOrigin();
} catch (error) {
  console.error('Error resolving the API base URL:', describeFailure(error));
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
 * SECURITY: remove the bearer token from a failed request. Axios keeps the request
 * configuration on the error, so without this the token travels to logs and callers on the
 * error object.
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

const NOT_AN_API_RESPONSE =
  'The API returned a response that is not JSON. The configured API base URL is answering ' +
  'with something other than the API - a static origin answers an unknown path with the ' +
  'application document - so the body was refused instead of being treated as API data.';

/**
 * Pass a JSON response through; reject one that declares any other content type.
 *
 * SECURITY: a successful response that is not JSON is refused — a static origin answers an
 * unmatched path with the application document under status 200, and that document was
 * returned to callers as though it were workbook data.
 *
 * A response declaring no content type is passed through, so nothing is refused on the
 * strength of a missing header alone.
 */
function assertJsonResponse<T extends { headers?: unknown }>(response: T): T {
  const headers = response.headers;
  if (typeof headers !== 'object' || headers === null) {
    return response;
  }
  const bag = headers as Record<string, unknown> & { get?: (name: string) => unknown };
  const name = Object.keys(bag).find((key) => key.toLowerCase() === 'content-type');
  const declared =
    name === undefined && typeof bag.get === 'function'
      ? bag.get('content-type')
      : name === undefined
        ? undefined
        : bag[name];
  if (typeof declared !== 'string' || declared === '') {
    return response;
  }
  if (!declared.toLowerCase().includes('json')) {
    throw new Error(NOT_AN_API_RESPONSE);
  }
  return response;
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

// SECURITY: the token-attaching interceptor is confined to this client's calls — on the
// global `axios` default it attached the credential to every axios call in the bundle.
const apiClient: AxiosInstance = axios.create({ baseURL: API_BASE_URL });

// SECURITY: attach the current Firebase ID token so the server can verify the caller.
const authorizationRequestInterceptorId = apiClient.interceptors.request.use(
  async (config: InternalAxiosRequestConfig) => {
    // SECURITY: a request is refused here rather than sent to an origin that serves no API.
    assertApiBaseUrlIsAnApiOrigin();
    const auth = await resolvedAuthentication();
    const token = await auth.currentUser?.getIdToken();
    if (!token) {
      // SECURITY: an API request is refused here rather than sent unauthenticated.
      throw new Error(NOT_AUTHENTICATED);
    }
    // SECURITY: any header already carrying a credential is removed first — assigning one
    // casing left a differently cased one in place, so a stale token could be sent alongside
    // the current one, or instead of it.
    deleteAuthorizationHeader(config.headers);
    config.headers[AUTHORIZATION_HEADER] = `Bearer ${token}`;
    return config;
  }
);

const authorizationErrorInterceptorId = apiClient.interceptors.response.use(
  // SECURITY: a refusal raised on the SUCCESS path is redacted by the same rule as a transport
  // failure. Throwing from the fulfilled handler sent the error straight to the caller without
  // passing through the rejected handler, so the redaction guarantee did not cover it - and the
  // request configuration attached below carries the bearer token.
  (response) => {
    try {
      return assertJsonResponse(response);
    } catch (thrown) {
      const failure = thrown as Error & { config?: unknown; response?: unknown };
      failure.config = (response as { config?: unknown }).config;
      failure.response = response;
      return Promise.reject(redactAuthorizationHeader(failure));
    }
  },
  // The classification runs BEFORE redaction so it can read the response status and the
  // Retry-After header, and redaction then removes the credential from what is handed on.
  (error: unknown) => Promise.reject(redactAuthorizationHeader(classifyFailure(error)))
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
 * Key identifying the batch a cell write belongs to. The separator is NUL, which a URL path
 * segment cannot carry; the ids are not validated here, so key uniqueness assumes neither id
 * contains a NUL byte.
 */
// SECURITY: encode a value before it becomes one path segment of a request URL - an
// identifier is caller-supplied, and interpolated raw a value containing / or ? or # changes
// which resource the request addresses.
function pathSegment(value: string): string {
  return encodeURIComponent(value);
}

function cellWriteKey(workbookId: string, worksheetId: string): string {
  return `${workbookId}\u0000${worksheetId}`;
}

/**
 * Send every cell queued for one worksheet as a single request, then settle its callers.
 *
 * The batch is removed from the queue before the request is issued, so writes arriving while
 * it is in flight accumulate into the next batch rather than joining one already sent.
 *
 * Each caller is settled with the cell it supplied: the route acknowledges with a message
 * rather than returning cells, so no server-side cell value exists to return.
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
    // CONTRACT: the route takes a list of cells and answers with an acknowledgement message
    // rather than cells, so each caller is settled with the cell it supplied.
    await apiClient.put(
      `/workbooks/${pathSegment(workbookId)}/worksheets/${pathSegment(worksheetId)}/cells`,
      waiters.map((waiter) => waiter.cell)
    );
    waiters.forEach((waiter) => waiter.resolve(waiter.cell));
  } catch (error) {
    console.error('Error updating cells:', describeFailure(error));
    waiters.forEach((waiter) => waiter.reject(error));
  }
}

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
