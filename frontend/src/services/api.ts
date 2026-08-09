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
// The refusal this client makes on its own account, when no credential exists to send. It is
// the one case that most warrants a sign-in prompt, and it arrived at pages as an
// unclassified generic failure: `apiFailure` returned undefined, so a page following the
// working 401 pattern read `reauthenticate` off undefined and threw.
const SIGN_IN_REQUIRED = 'You are not signed in. Sign in to continue.';

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

/**
 * The refusal to fail a caller with when no identity token exists to send.
 *
 * SECURITY: the classification is attached here rather than by the response interceptor
 * because this refusal is raised from the REQUEST interceptor, and a request-interceptor
 * rejection never reaches a response interceptor - so the classification the pages read has
 * to travel on the error from the moment it is created.
 */
function notAuthenticatedError(): Error {
  const refusal = new Error(NOT_AUTHENTICATED) as Error & Record<string, unknown>;
  const classified: ApiFailure = { userMessage: SIGN_IN_REQUIRED, reauthenticate: true };
  refusal[API_FAILURE] = classified;
  return refusal;
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

// A batch refused with 429 is re-queued once and sent again after the window the server
// advertised, clamped into this range: below the coalescing window the retry would arrive
// inside the same budget that just refused it, and an unbounded upper end would leave a
// caller's promise unsettled for as long as a server cared to name.
const CELL_WRITE_RETRY_MIN_MS = CELL_WRITE_COALESCE_MS;
const CELL_WRITE_RETRY_MAX_MS = 60_000;

// The keepalive budget a browser allows across all in-flight keepalive requests is 64 KiB.
// A batch whose body exceeds this is sent without keepalive rather than being rejected
// outright: it may not survive the unload, but the alternative is not sending it at all.
const KEEPALIVE_BODY_LIMIT_BYTES = 60_000;

interface CellWriteWaiter {
  cell: CellSchema;
  resolve: (cell: CellSchema) => void;
  reject: (reason: unknown) => void;
}

interface PendingCellWrites {
  // Carried on the batch so a flush never has to recover them from the key.
  workbookId: string;
  worksheetId: string;
  waiters: CellWriteWaiter[];
  // The DOM and Node typings disagree on what setTimeout returns, so the handle type is
  // derived from the function rather than named.
  timer: ReturnType<typeof setTimeout> | null;
  // Whether this batch has already been re-queued after a 429. One retry, then the callers
  // are told, so a throttled write can neither be lost silently nor retried indefinitely.
  retried: boolean;
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

const NOT_A_WORKBOOK_COLLECTION =
  'The API returned a body that is not a list of workbooks. It was refused rather than ' +
  'passed on, because a body of the wrong shape reaches the interface as workbook data and ' +
  'fails much later, somewhere that cannot explain it.';

const NOT_A_WORKBOOK =
  'The API returned a body that is not a workbook. It was refused rather than passed on, ' +
  'because a body of the wrong shape reaches the interface as a workbook and fails much ' +
  'later, somewhere that cannot explain it.';

/** Whether `value` is a non-null, non-array object. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Whether `value` satisfies Pydantic `Dict[str, str]`. */
function isStringRecord(value: unknown): boolean {
  return isRecord(value) && Object.keys(value).every((key) => typeof value[key] === 'string');
}

/** Whether `value` satisfies Pydantic `Optional[Dict[str, str]]`. */
function isOptionalStringRecord(value: unknown): boolean {
  return value === null || value === undefined || isStringRecord(value);
}

/** Whether `value` satisfies `CellSchema`: `value: str`, `formula: Optional[str]`, `style: Dict[str, str]`. */
function isCellSchema(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  const formula = value.formula;
  return (
    typeof value.value === 'string' &&
    (formula === null || formula === undefined || typeof formula === 'string') &&
    isStringRecord(value.style)
  );
}

/** Whether `value` satisfies `WorksheetSchema`. The frozen model carries no identifier field. */
function isWorksheetSchema(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  const cells = value.cells;
  return (
    typeof value.name === 'string' &&
    isRecord(cells) &&
    Object.keys(cells).every((reference) => isCellSchema(cells[reference])) &&
    isOptionalStringRecord(value.named_ranges)
  );
}

/**
 * Whether `value` satisfies `WorkbookSchema`.
 *
 * Every field the frozen Pydantic model declares is checked for the type it declares; a field
 * it does not declare is tolerated, so a server that adds one does not break this client.
 * `created_at` and `modified_at` are datetimes, which FastAPI serialises as strings.
 */
function isWorkbookSchema(value: unknown): boolean {
  if (!isRecord(value)) {
    return false;
  }
  const worksheets = value.worksheets;
  return (
    typeof value.id === 'string' &&
    typeof value.name === 'string' &&
    typeof value.owner_id === 'string' &&
    Array.isArray(worksheets) &&
    worksheets.every(isWorksheetSchema) &&
    typeof value.created_at === 'string' &&
    typeof value.modified_at === 'string' &&
    isOptionalStringRecord(value.settings)
  );
}

/**
 * Return `data` as a list of workbooks, or throw.
 *
 * SECURITY: a 2xx body is not evidence of a well-formed body. A truncated JSON document
 * arrives as a bare string, a JSON `null` arrives as null, and a body carrying numbers where
 * the contract declares strings arrives as an object - and every one of those resolved as
 * though it were workbook data, so the first sign of trouble was a TypeError in unrelated
 * code, or wrong types silently entering the domain model. The refusal names no part of the
 * body, so a server-side detail cannot travel to the interface through this path.
 */
function assertWorkbookCollection(data: unknown): WorkbookSchema[] {
  if (!Array.isArray(data) || !data.every(isWorkbookSchema)) {
    throw new Error(NOT_A_WORKBOOK_COLLECTION);
  }
  return data as WorkbookSchema[];
}

/** Return `data` as one workbook, or throw. See {@link assertWorkbookCollection}. */
function assertWorkbook(data: unknown): WorkbookSchema {
  if (!isWorkbookSchema(data)) {
    throw new Error(NOT_A_WORKBOOK);
  }
  return data as WorkbookSchema;
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
      throw notAuthenticatedError();
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

// Every registration belongs to this module instance, so a development reload discards them
// instead of leaving a further set behind - the two interceptors on the client, and the
// document handlers that flush queued cell writes as the page is left.
const hotModuleApi = typeof module === 'undefined' ? undefined : module?.hot;
if (hotModuleApi?.dispose) {
  hotModuleApi.dispose(() => {
    apiClient.interceptors.request.eject(authorizationRequestInterceptorId);
    apiClient.interceptors.response.eject(authorizationErrorInterceptorId);
    removeUnloadHooks();
  });
}

// CONTRACT: `GET /workbooks` is a paged route - `backend/app/api/workbooks.py` declares
// `skip: int = 0, limit: int = 100`, so a request that names neither receives the FIRST 100
// workbooks and no indication that more exist. Asking for one page at a time and stopping on
// a short page is what makes the collection complete rather than truncated at the route's
// default.
const WORKBOOK_PAGE_SIZE = 100;

// An upper bound on the paging loop. A server that ignores `skip` - or answers a full page
// forever - would otherwise be walked until the tab ran out of memory, so the loop stops and
// says so. The bound admits WORKBOOK_PAGE_SIZE * WORKBOOK_PAGE_LIMIT workbooks.
const WORKBOOK_PAGE_LIMIT = 100;

const TOO_MANY_WORKBOOKS =
  `The API is still returning workbooks after ${WORKBOOK_PAGE_SIZE * WORKBOOK_PAGE_LIMIT} of ` +
  'them. Paging stopped there rather than continuing until this tab ran out of memory.';

export const fetchWorkbooks = async (): Promise<WorkbookSchema[]> => {
  try {
    const workbooks: WorkbookSchema[] = [];
    for (let page = 0; page < WORKBOOK_PAGE_LIMIT; page += 1) {
      const response = await apiClient.get('/workbooks', {
        params: { skip: workbooks.length, limit: WORKBOOK_PAGE_SIZE },
      });
      const batch = assertWorkbookCollection(response.data);
      workbooks.push(...batch);
      if (batch.length < WORKBOOK_PAGE_SIZE) {
        return workbooks;
      }
    }
    throw new Error(TOO_MANY_WORKBOOKS);
  } catch (error) {
    console.error('Error fetching workbooks:', describeFailure(error));
    throw error;
  }
};

export const createWorkbook = async (workbook: WorkbookSchema): Promise<WorkbookSchema> => {
  try {
    const response = await apiClient.post('/workbooks', workbook);
    return assertWorkbook(response.data);
  } catch (error) {
    console.error('Error creating workbook:', describeFailure(error));
    throw error;
  }
};

// SECURITY: encode a value before it becomes one path segment of a request URL - an
// identifier is caller-supplied, and interpolated raw a value containing / or ? or # changes
// which resource the request addresses.
function pathSegment(value: string): string {
  return encodeURIComponent(value);
}

/** The cells route for one worksheet, relative to the API base URL. */
function cellWritePath(workbookId: string, worksheetId: string): string {
  return `/workbooks/${pathSegment(workbookId)}/worksheets/${pathSegment(worksheetId)}/cells`;
}

/**
 * Key identifying the batch a cell write belongs to.
 *
 * The encoding is injective whatever the identifiers contain. A NUL-separated key was not:
 * ("a\u0000b", "c") and ("a", "b\u0000c") produced the same key, so two writes addressed to
 * different worksheets merged into one request aimed at whichever of them opened the window,
 * the other worksheet's request was never issued, and both callers were told their write had
 * succeeded.
 */
function cellWriteKey(workbookId: string, worksheetId: string): string {
  return JSON.stringify([workbookId, worksheetId]);
}

/**
 * A value equal for two cells carrying the same content, whatever order their style
 * properties were written in.
 */
function cellIdentity(cell: CellSchema): string {
  const style = isRecord(cell.style) ? cell.style : {};
  const orderedStyle = Object.keys(style)
    .sort()
    .map((property) => [property, style[property]]);
  return JSON.stringify([cell.value, cell.formula ?? null, orderedStyle]);
}

/**
 * The cells to send for `waiters`, with duplicates collapsed.
 *
 * A caller submitting the same cell twice inside one window put two identical entries in the
 * request body. Only the last occurrence of each distinct cell is kept, which leaves the
 * order the route applies them in unchanged under last-write-wins; every caller is still
 * settled, including one whose cell was collapsed into another's.
 */
function cellWriteBody(waiters: CellWriteWaiter[]): CellSchema[] {
  const identities = waiters.map((waiter) => cellIdentity(waiter.cell));
  const lastOccurrence = new Map<string, number>();
  identities.forEach((identity, index) => {
    lastOccurrence.set(identity, index);
  });
  const cells: CellSchema[] = [];
  identities.forEach((identity, index) => {
    if (lastOccurrence.get(identity) === index) {
      cells.push(waiters[index].cell);
    }
  });
  return cells;
}

/**
 * Remove the batch queued under `key`, and stop its timer, when it is still `batch`.
 *
 * Returns whether it was: a batch already sent has been replaced by whatever arrived while it
 * was in flight, and that later batch is not this caller's to settle.
 */
function takeCellWriteBatch(key: string, batch: PendingCellWrites): boolean {
  if (pendingCellWrites.get(key) !== batch) {
    return false;
  }
  pendingCellWrites.delete(key);
  if (batch.timer !== null) {
    clearTimeout(batch.timer);
    batch.timer = null;
  }
  return true;
}

/** How long to wait before re-sending a throttled batch, from the window the server named. */
function retryDelayMs(error: unknown): number {
  const advertised = apiFailure(error)?.retryAfterSeconds;
  const requested = advertised === undefined ? CELL_WRITE_RETRY_MIN_MS : advertised * 1000;
  return Math.min(Math.max(requested, CELL_WRITE_RETRY_MIN_MS), CELL_WRITE_RETRY_MAX_MS);
}

/**
 * Queue a throttled batch to be sent once more, after `delayMs`.
 *
 * The refused writes were queued before anything that arrived while the request was in
 * flight, so they stay in front of it. The combined batch is marked as retried, so a second
 * refusal settles its callers rather than starting an unbounded cycle.
 */
function requeueCellWrites(batch: PendingCellWrites, delayMs: number): void {
  const key = cellWriteKey(batch.workbookId, batch.worksheetId);
  const arrivedInFlight = pendingCellWrites.get(key);
  if (arrivedInFlight !== undefined && arrivedInFlight.timer !== null) {
    clearTimeout(arrivedInFlight.timer);
    arrivedInFlight.timer = null;
  }
  const retry: PendingCellWrites = {
    workbookId: batch.workbookId,
    worksheetId: batch.worksheetId,
    waiters:
      arrivedInFlight === undefined
        ? batch.waiters
        : batch.waiters.concat(arrivedInFlight.waiters),
    timer: null,
    retried: true,
  };
  pendingCellWrites.set(key, retry);
  retry.timer = setTimeout(() => {
    void flushCellWrites(retry.workbookId, retry.worksheetId);
  }, delayMs);
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
  takeCellWriteBatch(key, pending);
  const { waiters } = pending;
  try {
    // CONTRACT: the route takes a list of cells and answers with an acknowledgement message
    // rather than cells, so each caller is settled with the cell it supplied.
    await apiClient.put(cellWritePath(workbookId, worksheetId), cellWriteBody(waiters));
    waiters.forEach((waiter) => waiter.resolve(waiter.cell));
  } catch (error) {
    // A throttled batch was dropped whole, so every cell in it was lost from the client's
    // point of view. It is sent once more after the window the server advertised instead.
    if (!pending.retried && apiFailure(error)?.status === TOO_MANY_REQUESTS_STATUS) {
      const delayMs = retryDelayMs(error);
      console.warn('Retrying throttled cell writes:', { delayMs, ...describeFailure(error) });
      requeueCellWrites(pending, delayMs);
      return;
    }
    console.error('Error updating cells:', describeFailure(error));
    waiters.forEach((waiter) => waiter.reject(error));
  }
}

const NO_UNLOAD_TRANSPORT =
  'This browser offers no request that outlives the document, so the cell writes still ' +
  'queued when the page was left could not be sent.';

const CELL_WRITE_REFUSED_ON_UNLOAD = (status: number): string =>
  `The API refused the cell writes sent as the page was left, with status ${status}.`;

/** Total cell writes waiting across every queued batch. */
function pendingCellWriteCount(): number {
  let waiting = 0;
  pendingCellWrites.forEach((batch) => {
    waiting += batch.waiters.length;
  });
  return waiting;
}

/** `path` resolved against the configured API base URL, which is absolute by contract. */
function absoluteApiUrl(path: string): string {
  return `${API_BASE_URL.replace(/\/+$/, '')}${path}`;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/** The document's `fetch`, or undefined where there is none to use. */
function unloadFetch(): FetchLike | undefined {
  if (typeof window === 'undefined') {
    return undefined;
  }
  const candidate = (window as unknown as Record<string, unknown>).fetch;
  return typeof candidate === 'function' ? (candidate as FetchLike) : undefined;
}

/** The size of `body` on the wire, which is what the keepalive budget is measured in. */
function bodyByteLength(body: string): number {
  if (typeof TextEncoder === 'undefined') {
    return body.length;
  }
  return new TextEncoder().encode(body).length;
}

/**
 * Send one queued batch with a request that outlives the document, then settle its callers.
 *
 * SECURITY: the credential is attached here by hand, and the API origin re-asserted, because
 * this request does not travel through the client's interceptors. Cookies are omitted: the
 * request is authenticated by the bearer token and nothing else should authenticate it.
 * `navigator.sendBeacon` is not used because it cannot carry an Authorization header at all,
 * so it would send this data unauthenticated.
 */
async function sendCellWritesOnUnload(batch: PendingCellWrites): Promise<void> {
  const { waiters } = batch;
  try {
    assertApiBaseUrlIsAnApiOrigin();
    const send = unloadFetch();
    if (send === undefined) {
      throw new Error(NO_UNLOAD_TRANSPORT);
    }
    const auth = await resolvedAuthentication();
    const token = await auth.currentUser?.getIdToken();
    if (!token) {
      throw notAuthenticatedError();
    }
    const body = JSON.stringify(cellWriteBody(waiters));
    const response = await send(
      absoluteApiUrl(cellWritePath(batch.workbookId, batch.worksheetId)),
      {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          [AUTHORIZATION_HEADER]: `Bearer ${token}`,
        },
        body,
        keepalive: bodyByteLength(body) <= KEEPALIVE_BODY_LIMIT_BYTES,
        credentials: 'omit',
        cache: 'no-store',
      }
    );
    if (!response.ok) {
      throw new Error(CELL_WRITE_REFUSED_ON_UNLOAD(response.status));
    }
    waiters.forEach((waiter) => waiter.resolve(waiter.cell));
  } catch (error) {
    console.error('Error updating cells while leaving the page:', describeFailure(error));
    waiters.forEach((waiter) => waiter.reject(error));
  }
}

/**
 * Send every queued batch with a request that outlives the document.
 *
 * The queue is emptied before anything is sent, so the second unload event of a navigation -
 * `visibilitychange` and `pagehide` both fire - finds nothing left and cannot duplicate a
 * write.
 */
function flushCellWritesOnUnload(): void {
  if (pendingCellWrites.size === 0) {
    return;
  }
  const batches = Array.from(pendingCellWrites.values());
  pendingCellWrites.clear();
  batches.forEach((batch) => {
    if (batch.timer !== null) {
      clearTimeout(batch.timer);
      batch.timer = null;
    }
    void sendCellWritesOnUnload(batch);
  });
}

interface UnloadHook {
  target: EventTarget;
  type: string;
  listener: EventListener;
}

const unloadHooks: UnloadHook[] = [];

/**
 * Register the handlers that stop a queued cell write from being discarded with the document.
 *
 * A write queued inside the coalescing window was lost outright when the page was left: the
 * timer was cleared with the document, nothing was sent, and the caller's promise never
 * settled - no error, no console output, no prompt. `pagehide` covers a navigation and
 * `visibilitychange` covers the cases a browser fires no `pagehide` for, such as a
 * backgrounded tab being terminated. `beforeunload` is the user's own signal, and it is armed
 * only while something is actually queued, so a page with nothing to save never prompts.
 */
function registerUnloadHooks(): void {
  if (typeof window === 'undefined' || typeof window.addEventListener !== 'function') {
    return;
  }
  const hooks: UnloadHook[] = [
    {
      target: window,
      type: 'pagehide',
      listener: () => {
        flushCellWritesOnUnload();
      },
    },
    {
      target: window,
      type: 'beforeunload',
      listener: (event: Event) => {
        if (pendingCellWriteCount() === 0) {
          return;
        }
        event.preventDefault();
        (event as BeforeUnloadEvent).returnValue = '';
      },
    },
  ];
  if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    hooks.push({
      target: document,
      type: 'visibilitychange',
      listener: () => {
        if (document.visibilityState === 'hidden') {
          flushCellWritesOnUnload();
        }
      },
    });
  }
  hooks.forEach((hook) => {
    hook.target.addEventListener(hook.type, hook.listener);
    unloadHooks.push(hook);
  });
}

/** Discard the handlers this module instance registered. */
function removeUnloadHooks(): void {
  unloadHooks.splice(0).forEach((hook) => {
    hook.target.removeEventListener(hook.type, hook.listener);
  });
}

registerUnloadHooks();

/**
 * Resolve to the refusal a cell write must fail with, or to undefined when a credential is
 * available to send.
 *
 * The interceptor makes the same check, but it makes it when the batch is flushed - so a cell
 * edited while signed out failed a whole coalescing window after the user acted. This runs at
 * the moment of the edit instead.
 */
async function cellWriteRefusal(): Promise<Error | undefined> {
  try {
    assertApiBaseUrlIsAnApiOrigin();
    const auth = await resolvedAuthentication();
    const token = await auth.currentUser?.getIdToken();
    return token ? undefined : notAuthenticatedError();
  } catch (error) {
    return error instanceof Error ? error : notAuthenticatedError();
  }
}

export const updateCell = (workbookId: string, worksheetId: string, cell: CellSchema): Promise<CellSchema> =>
  new Promise<CellSchema>((resolve, reject) => {
    const key = cellWriteKey(workbookId, worksheetId);
    let pending = pendingCellWrites.get(key);
    if (pending === undefined) {
      pending = { workbookId, worksheetId, waiters: [], timer: null, retried: false };
      pendingCellWrites.set(key, pending);
    }
    pending.waiters.push({ cell, resolve, reject });
    if (pending.timer === null) {
      pending.timer = setTimeout(() => {
        void flushCellWrites(workbookId, worksheetId);
      }, CELL_WRITE_COALESCE_MS);
    }
    // Queueing stays synchronous, so writes keep the order they were made in. The credential
    // check that follows is the only asynchronous part, and it is held against the batch this
    // write actually joined, so a refusal arriving after that batch has been sent cannot
    // fail the next one.
    const batch = pending;
    void cellWriteRefusal().then((refusal) => {
      if (refusal === undefined || !takeCellWriteBatch(key, batch)) {
        return;
      }
      console.error('Error updating cells:', describeFailure(refusal));
      batch.waiters.forEach((waiter) => waiter.reject(refusal));
    });
  });
