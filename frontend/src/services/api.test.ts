/**
 * Verification of the client half of the identity bridge in `api.ts`.
 *
 * The module has import-time side effects: it initialises the Firebase app and registers
 * one request and one response interceptor on its own Axios instance. Each test therefore
 * loads a fresh copy through `loadApiModule`, which transpiles `api.ts` with the declared
 * `typescript` dependency and supplies fakes for `axios`, `firebase/app` and
 * `firebase/auth`, plus a `module` object whose `hot` field is controllable. Nothing here
 * reaches the network, Firebase or the API.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';

const API_BASE_URL = 'https://api.test.example';

/**
 * A complete `CellSchema`: every field `backend/app/schema/workbook_schema.py` declares, and
 * no field it does not.
 */
const CELL_DTO = {
  value: '42',
  formula: '=SUM(A1:A2)',
  style: { fontWeight: 'bold' },
};

/**
 * A complete `WorkbookSchema`, including a complete nested `WorksheetSchema`. Used wherever a
 * request body or a response payload stands in for the frozen contract, so the assertion is
 * proof against the real model rather than against a partial object.
 */
const WORKBOOK_DTO = {
  id: 'wb-1',
  name: 'Test Workbook',
  owner_id: 'owner-1',
  worksheets: [
    {
      name: 'Sheet1',
      cells: { A1: CELL_DTO },
      named_ranges: null,
    },
  ],
  created_at: '2026-01-01T00:00:00Z',
  modified_at: '2026-01-02T03:04:05Z',
  settings: null,
};

/** Firebase web configuration values. All are public by design; none is a secret. */
const FIREBASE_ENVIRONMENT: Record<string, string> = {
  REACT_APP_API_BASE_URL: API_BASE_URL,
  REACT_APP_FIREBASE_API_KEY: 'test-api-key',
  REACT_APP_FIREBASE_AUTH_DOMAIN: 'test-project.firebaseapp.com',
  REACT_APP_FIREBASE_PROJECT_ID: 'test-project',
  REACT_APP_FIREBASE_APP_ID: 'test-app-id',
};

type Recorded = string[];

interface FakeInterceptorSlot {
  use: (fulfilled?: any, rejected?: any) => number;
  eject: (id: number) => void;
  __handlers: Array<{ fulfilled?: any; rejected?: any } | null>;
}

interface FakeAxiosInstance {
  __name: string;
  __sent: any[];
  __nextResponse: { data?: any; error?: any; status?: number; headers?: any } | null;
  interceptors: { request: FakeInterceptorSlot; response: FakeInterceptorSlot };
  defaults: Record<string, any>;
  request: (config: any) => Promise<any>;
  get: (url: string, config?: any) => Promise<any>;
  post: (url: string, data?: any, config?: any) => Promise<any>;
  put: (url: string, data?: any, config?: any) => Promise<any>;
}

function makeInterceptorSlot(): FakeInterceptorSlot {
  const handlers: Array<{ fulfilled?: any; rejected?: any } | null> = [];
  return {
    __handlers: handlers,
    use(fulfilled?: any, rejected?: any): number {
      handlers.push({ fulfilled, rejected });
      return handlers.length - 1;
    },
    eject(id: number): void {
      handlers[id] = null;
    },
  };
}

/**
 * An Axios stand-in that really runs the registered interceptors.
 *
 * A request passes through every request interceptor, is recorded, and then resolves from
 * `__nextResponse`; a rejection passes through every response error interceptor. That is
 * what makes "the request was never dispatched" and "the token was redacted" observable.
 */
function makeFakeAxiosInstance(name: string, config: any = {}): FakeAxiosInstance {
  const request = makeInterceptorSlot();
  const response = makeInterceptorSlot();

  const instance: FakeAxiosInstance = {
    __name: name,
    __sent: [],
    __nextResponse: null,
    interceptors: { request, response },
    defaults: { ...config },
    async request(requestConfig: any): Promise<any> {
      let resolved = { ...requestConfig, headers: { ...(requestConfig.headers || {}) } };
      try {
        for (const handler of request.__handlers) {
          if (handler && handler.fulfilled) {
            resolved = await handler.fulfilled(resolved);
          }
        }
      } catch (error) {
        return runResponseErrorChain(error);
      }
      instance.__sent.push(resolved);
      const outcome = instance.__nextResponse || { data: null };
      if (outcome.error) {
        const failure: any = outcome.error;
        failure.config = resolved;
        return runResponseErrorChain(failure);
      }
      let success: any = {
        status: outcome.status || 200,
        data: outcome.data,
        config: resolved,
        headers: outcome.headers || {},
      };
      try {
        for (const handler of response.__handlers) {
          if (handler && handler.fulfilled) {
            success = await handler.fulfilled(success);
          }
        }
      } catch (error) {
        return runResponseErrorChain(error);
      }
      return success;
    },
    get(url: string, requestConfig?: any): Promise<any> {
      return instance.request({ ...requestConfig, method: 'get', url });
    },
    post(url: string, data?: any, requestConfig?: any): Promise<any> {
      return instance.request({ ...requestConfig, method: 'post', url, data });
    },
    put(url: string, data?: any, requestConfig?: any): Promise<any> {
      return instance.request({ ...requestConfig, method: 'put', url, data });
    },
  };

  function runResponseErrorChain(error: unknown): Promise<never> {
    let chain: Promise<any> = Promise.reject(error);
    for (const handler of response.__handlers) {
      if (handler && handler.rejected) {
        chain = chain.catch(handler.rejected);
      }
    }
    return chain as Promise<never>;
  }

  return instance;
}

interface Harness {
  api: any;
  /** The module-level `axios` object; nothing may be registered on it. */
  axiosDefault: FakeAxiosInstance;
  /** Instances produced by `axios.create`. */
  created: FakeAxiosInstance[];
  /** The dedicated client the module builds. */
  client: FakeAxiosInstance;
  /** Call order across the Firebase fakes, in the sequence the module invoked them. */
  order: Recorded;
  /** Console output the module produced, so a failure can be inspected for leakage. */
  logged: string[];
  /** Resolves the persisted-session restore. */
  settleAuthState: () => void;
  setCurrentUser: (user: any | null) => void;
  /** Disposers registered through `module.hot`. */
  disposers: Array<() => void>;
  /** Configuration passed to each `initializeApp` call. */
  initialisedWith: any[];
  /** The app instance each `getAuth` call was given. */
  authResolvedFor: any[];
  /** The default app instance the fakes hand out. */
  appInstance: any;
}

interface LoadOptions {
  /** Whether a signed-in user exists once the auth state has settled. */
  token?: string | null;
  /** Leave the persisted-session restore pending until `settleAuthState` is called. */
  deferAuthState?: boolean;
  /** Supply `module.hot`, as a development build does. */
  withHotModuleReplacement?: boolean;
  /** Environment overrides; omit a Firebase key to simulate an incomplete build. */
  environment?: Record<string, string | undefined>;
}

let transpiledApiSource: string | null = null;

/**
 * Transpile `api.ts` once per test run.
 *
 * The transpiled text is returned from a local `const`, and the module-level cache is only
 * written. That keeps the return type `string` outright rather than relying on the compiler
 * narrowing a nullable module-level `let` across the assignment that fills it - narrowing
 * that holds today only because the guard, the assignment and the return sit in this one
 * function, and that would stop holding if any of them moved or this became `async`. A
 * non-null assertion would silence the same risk by suppressing the check that reports it.
 */
function apiSource(): string {
  if (transpiledApiSource !== null) {
    return transpiledApiSource;
  }
  const source = fs.readFileSync(path.join(__dirname, 'api.ts'), 'utf8');
  const transpiled = ts.transpileModule(source, {
    fileName: 'api.ts',
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2019,
      esModuleInterop: true,
    },
  }).outputText;
  transpiledApiSource = transpiled;
  return transpiled;
}

function loadApiModule(options: LoadOptions = {}): Harness {
  const order: Recorded = [];
  const logged: string[] = [];
  const created: FakeAxiosInstance[] = [];
  const disposers: Array<() => void> = [];

  const axiosDefault = makeFakeAxiosInstance('module-default');
  const axiosFake: any = axiosDefault;
  axiosFake.__esModule = true;
  axiosFake.create = (config: any) => {
    const instance = makeFakeAxiosInstance('created', config);
    created.push(instance);
    return instance;
  };
  axiosFake.isAxiosError = (error: any) => Boolean(error && error.__isAxiosError);
  axiosFake.default = axiosFake;

  let currentUser: any = null;
  if (options.token !== undefined && options.token !== null) {
    currentUser = {
      getIdToken: () => {
        order.push('getIdToken');
        return Promise.resolve(options.token);
      },
    };
  }

  let settle: () => void = () => undefined;
  const authStateSettled = options.deferAuthState
    ? new Promise<void>((resolve) => {
        settle = resolve;
      })
    : Promise.resolve();

  const auth = {
    get currentUser() {
      return currentUser;
    },
    authStateReady: () => {
      order.push('authStateReady');
      return authStateSettled;
    },
  };

  const apps: any[] = [];
  const appInstance = { name: '[DEFAULT]' };
  const initialisedWith: any[] = [];
  const authResolvedFor: any[] = [];
  const firebaseAppFake = {
    __esModule: true,
    getApps: () => apps,
    getApp: () => {
      order.push('getApp');
      return appInstance;
    },
    initializeApp: (config: any) => {
      order.push('initializeApp');
      initialisedWith.push(config);
      apps.push(appInstance);
      return appInstance;
    },
  };
  const firebaseAuthFake = {
    __esModule: true,
    getAuth: (app: any) => {
      order.push('getAuth');
      authResolvedFor.push(app);
      return auth;
    },
  };

  const environment = { ...FIREBASE_ENVIRONMENT, ...(options.environment || {}) };
  const consoleFake = {
    error: (...args: any[]) => {
      logged.push(args.map((value) => JSON.stringify(value)).join(' '));
    },
    warn: (...args: any[]) => {
      logged.push(args.map((value) => JSON.stringify(value)).join(' '));
    },
    log: () => undefined,
  };

  const moduleObject: any = { exports: {} };
  if (options.withHotModuleReplacement) {
    moduleObject.hot = {
      dispose: (callback: () => void) => {
        disposers.push(callback);
      },
    };
  }

  const requireShim = (id: string): any => {
    if (id === 'axios') {
      return axiosFake;
    }
    if (id === 'firebase/app') {
      return firebaseAppFake;
    }
    if (id === 'firebase/auth') {
      return firebaseAuthFake;
    }
    // Type-only imports are elided; anything else resolves to an empty module.
    return {};
  };

  // eslint-disable-next-line no-new-func
  const factory = new Function(
    'module',
    'exports',
    'require',
    'process',
    'console',
    apiSource()
  );
  factory(moduleObject, moduleObject.exports, requireShim, { env: environment }, consoleFake);

  return {
    api: moduleObject.exports,
    axiosDefault,
    created,
    client: created[0],
    order,
    logged,
    settleAuthState: () => settle(),
    setCurrentUser: (user: any | null) => {
      currentUser = user;
    },
    disposers,
    initialisedWith,
    authResolvedFor,
    appInstance,
  };
}

function authorizationOf(config: any): string | undefined {
  const headers = config.headers || {};
  const name = Object.keys(headers).find((key) => key.toLowerCase() === 'authorization');
  return name === undefined ? undefined : headers[name];
}

describe('Firebase initialisation ordering', () => {
  it('initialises the default app as the module is evaluated, before any getAuth call', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(harness.order).toEqual(['initializeApp']);
    expect(harness.order).not.toContain('getAuth');
  });

  it('initialises the app the configured project names', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(harness.initialisedWith).toHaveLength(1);
    expect(harness.initialisedWith[0].projectId).toBe('test-project');
    expect(harness.initialisedWith[0].apiKey).toBe('test-api-key');
  });

  it('reuses the already initialised default app rather than initialising a second', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [] };
    await harness.api.fetchWorkbooks();
    expect(harness.order.filter((entry) => entry === 'initializeApp')).toHaveLength(1);
    expect(harness.order).toContain('getApp');
  });

  it('resolves authentication against that same default app', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [] };
    await harness.api.fetchWorkbooks();
    expect(harness.authResolvedFor).toEqual([harness.appInstance]);
  });

  it('reports an incomplete Firebase configuration without throwing at import', () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      environment: { REACT_APP_FIREBASE_API_KEY: undefined },
    });
    expect(harness.logged.join(' ')).toContain('Firebase');
    expect(harness.api.fetchWorkbooks).toBeInstanceOf(Function);
  });
});

describe('Authorization header attachment', () => {
  it('waits for the restored session before reading the current user', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [] };
    await harness.api.fetchWorkbooks();
    expect(harness.order.indexOf('authStateReady')).toBeLessThan(
      harness.order.indexOf('getIdToken')
    );
  });

  it('does not send a request issued while the session is still being restored', async () => {
    const harness = loadApiModule({ deferAuthState: true });
    harness.client.__nextResponse = { data: [] };
    const pending = harness.api.fetchWorkbooks();
    expect(harness.client.__sent).toHaveLength(0);

    harness.setCurrentUser({
      getIdToken: () => {
        harness.order.push('getIdToken');
        return Promise.resolve('a-restored-token');
      },
    });
    harness.settleAuthState();

    await pending;
    expect(harness.client.__sent).toHaveLength(1);
    expect(authorizationOf(harness.client.__sent[0])).toBe('Bearer a-restored-token');
  });

  it('attaches the current identity token as a bearer credential', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [] };
    await harness.api.fetchWorkbooks();
    expect(authorizationOf(harness.client.__sent[0])).toBe('Bearer an-id-token');
  });

  it('registers the credential interceptor only on its own client', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(harness.created).toHaveLength(1);
    expect(harness.axiosDefault.interceptors.request.__handlers).toHaveLength(0);
    expect(harness.axiosDefault.interceptors.response.__handlers).toHaveLength(0);
    expect(harness.client.interceptors.request.__handlers).toHaveLength(1);
    expect(harness.client.interceptors.response.__handlers).toHaveLength(1);
  });

  it('builds its client against the configured API base URL', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(harness.client.defaults.baseURL).toBe(API_BASE_URL);
  });

  it('replaces a differently cased credential rather than adding a second', async () => {
    const harness = loadApiModule({ token: 'a-current-token' });
    harness.client.__nextResponse = { data: [] };
    await harness.client.request({
      method: 'get',
      url: '/workbooks',
      headers: { authorization: 'Bearer a-stale-token', 'X-Kept': 'yes' },
    });
    const sent = harness.client.__sent[0];
    const credentials = Object.keys(sent.headers).filter(
      (name) => name.toLowerCase() === 'authorization'
    );
    expect(credentials).toHaveLength(1);
    expect(authorizationOf(sent)).toBe('Bearer a-current-token');
    expect(sent.headers['X-Kept']).toBe('yes');
  });

  it('removes a credential through a header bag that deletes case-insensitively', async () => {
    const harness = loadApiModule({ token: 'a-current-token' });
    harness.client.__nextResponse = { data: [] };
    const deleted: string[] = [];
    const headers: any = {
      authorization: 'Bearer a-stale-token',
      delete(name: string) {
        deleted.push(name);
        delete headers[name.toLowerCase()];
      },
    };
    await harness.client.request({ method: 'get', url: '/workbooks', headers });
    expect(deleted).toContain('Authorization');
    expect(authorizationOf(harness.client.__sent[0])).toBe('Bearer a-current-token');
  });
});

describe('Refusal to send an unauthenticated request', () => {
  it('rejects rather than dispatching when no identity token is available', async () => {
    const harness = loadApiModule({ token: null });
    harness.client.__nextResponse = { data: [] };
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('Not authenticated');
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('rejects every exported call when no identity token is available', async () => {
    const harness = loadApiModule({ token: null });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('Not authenticated');
    await expect(harness.api.createWorkbook(WORKBOOK_DTO)).rejects.toThrow(
      'Not authenticated'
    );
    await expect(harness.api.updateCell('wb-1', 'ws-1', CELL_DTO)).rejects.toThrow(
      'Not authenticated'
    );
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('rejects when the identity token is an empty string', async () => {
    const harness = loadApiModule({ token: 'unused' });
    harness.setCurrentUser({ getIdToken: () => Promise.resolve('') });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('Not authenticated');
    expect(harness.client.__sent).toHaveLength(0);
  });
});

describe('Refusal to send to an origin that serves no API', () => {
  /** jsdom serves the test document from this origin. */
  const DOCUMENT_ORIGIN = window.location.origin;

  it('rejects every call when the API base URL is unset', async () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      environment: { REACT_APP_API_BASE_URL: undefined },
    });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('REACT_APP_API_BASE_URL');
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('rejects a relative API base URL, which resolves against this origin', async () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      environment: { REACT_APP_API_BASE_URL: '/api' },
    });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('REACT_APP_API_BASE_URL');
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('rejects an API base URL naming the origin that serves this application', async () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      environment: { REACT_APP_API_BASE_URL: DOCUMENT_ORIGIN },
    });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow(
      'names the origin serving this application'
    );
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('rejects that origin however the base URL spells it', async () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      environment: { REACT_APP_API_BASE_URL: `${DOCUMENT_ORIGIN.toUpperCase()}/api/v1` },
    });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow(
      'names the origin serving this application'
    );
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('reports an unusable API base URL at import without throwing', () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      environment: { REACT_APP_API_BASE_URL: undefined },
    });
    expect(harness.logged.join(' ')).toContain('API base URL');
    expect(harness.api.fetchWorkbooks).toBeInstanceOf(Function);
  });

  it('sends normally against a separate API origin', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [] };
    await harness.api.fetchWorkbooks();
    expect(harness.client.__sent).toHaveLength(1);
  });

  it('refuses a 200 that carries the application document instead of API data', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = {
      data: '<!DOCTYPE html><html lang="en"><head></head><body></body></html>',
      headers: { 'content-type': 'text/html; charset=utf-8' },
    };
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('not JSON');
  });

  it('accepts a JSON response', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = {
      data: [WORKBOOK_DTO],
      headers: { 'Content-Type': 'application/json' },
    };
    await expect(harness.api.fetchWorkbooks()).resolves.toEqual([WORKBOOK_DTO]);
  });

  it('accepts a response that declares no content type', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [WORKBOOK_DTO] };
    await expect(harness.api.fetchWorkbooks()).resolves.toEqual([WORKBOOK_DTO]);
  });
});

describe('Credential redaction on failure', () => {
  /**
   * Arrange a failing request. The client attaches the real credential to `error.config`,
   * exactly as Axios does; `responseHeaders`, when supplied, is carried untouched on
   * `error.response.config` so the second redaction path is exercised on a real bag.
   */
  function failingHarness(responseHeaders?: any): Harness {
    const harness = loadApiModule({ token: 'an-id-token' });
    const failure: any = new Error('Request failed');
    failure.__isAxiosError = true;
    if (responseHeaders !== undefined) {
      failure.response = { status: 500, config: { headers: responseHeaders } };
    }
    harness.client.__nextResponse = { error: failure };
    return harness;
  }

  async function failedRequest(harness: Harness): Promise<any> {
    try {
      await harness.api.fetchWorkbooks();
    } catch (error) {
      return error;
    }
    throw new Error('the request was expected to fail');
  }

  it('removes the credential the interceptor attached to the failing request', async () => {
    const harness = failingHarness();
    const error = await failedRequest(harness);
    expect(harness.client.__sent).toHaveLength(1);
    expect(error.config).toBeDefined();
    expect(authorizationOf(error.config)).toBeUndefined();
  });

  it('removes a lower-cased credential and leaves other headers in place', async () => {
    const responseHeaders: any = { authorization: 'Bearer an-id-token', 'X-Trace': 'keep' };
    const harness = failingHarness(responseHeaders);
    await failedRequest(harness);
    expect(Object.keys(responseHeaders)).toEqual(['X-Trace']);
    expect(responseHeaders['X-Trace']).toBe('keep');
  });

  it('removes the credential through a header bag that deletes case-insensitively', async () => {
    const deleted: string[] = [];
    const responseHeaders: any = {
      AUTHORIZATION: 'Bearer an-id-token',
      delete: (name: string) => {
        deleted.push(name);
        delete responseHeaders.AUTHORIZATION;
      },
    };
    const harness = failingHarness(responseHeaders);
    await failedRequest(harness);
    expect(deleted).toEqual(['Authorization']);
    expect(responseHeaders.AUTHORIZATION).toBeUndefined();
  });

  it('removes the credential from both the request and the response configuration', async () => {
    const responseHeaders: any = { Authorization: 'Bearer an-id-token' };
    const harness = failingHarness(responseHeaders);
    const error = await failedRequest(harness);
    expect(authorizationOf(error.config)).toBeUndefined();
    expect(Object.keys(responseHeaders)).toHaveLength(0);
  });

  it('logs no credential when a request fails', async () => {
    const harness = failingHarness();
    await failedRequest(harness);
    const output = harness.logged.join(' ');
    expect(output).not.toContain('an-id-token');
    expect(output.toLowerCase()).not.toContain('authorization');
  });

  it('redacts a refusal raised on the success path, not only a transport failure', async () => {
    // The non-JSON refusal is thrown from the FULFILLED handler. Before the fix that error
    // bypassed the rejected handler entirely, so the credential the request interceptor had
    // just attached travelled to the caller on `error.config`.
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = {
      data: '<!doctype html>',
      headers: { 'content-type': 'text/html; charset=utf-8' },
    };
    const error = await failedRequest(harness);
    expect(error.config).toBeDefined();
    expect(authorizationOf(error.config)).toBeUndefined();
    expect(harness.logged.join(' ')).not.toContain('an-id-token');
  });
});

describe('Deliberate refusals are distinguishable from a generic failure', () => {
  /** Arrange a failing request whose response carries `status` and `headers`. */
  function refusingHarness(status: number, headers: any = {}): Harness {
    const harness = loadApiModule({ token: 'an-id-token' });
    const failure: any = new Error('Request failed');
    failure.__isAxiosError = true;
    failure.response = { status, headers, config: {} };
    harness.client.__nextResponse = { error: failure };
    return harness;
  }

  async function refusal(harness: Harness): Promise<any> {
    try {
      await harness.api.fetchWorkbooks();
    } catch (error) {
      return error;
    }
    throw new Error('the request was expected to fail');
  }

  it('marks a 401 as needing re-authentication', async () => {
    const harness = refusingHarness(401);
    const error = await refusal(harness);
    const classified = harness.api.apiFailure(error);
    expect(classified).toBeDefined();
    expect(classified.status).toBe(401);
    expect(classified.reauthenticate).toBe(true);
    expect(classified.userMessage).toMatch(/sign in again/i);
  });

  it('marks a 403 as a permission problem rather than a credential problem', async () => {
    const harness = refusingHarness(403);
    const classified = harness.api.apiFailure(await refusal(harness));
    expect(classified.status).toBe(403);
    expect(classified.reauthenticate).toBe(false);
  });

  it('reports the throttle window the server advertised', async () => {
    const harness = refusingHarness(429, { 'Retry-After': '45' });
    const classified = harness.api.apiFailure(await refusal(harness));
    expect(classified.status).toBe(429);
    expect(classified.reauthenticate).toBe(false);
    expect(classified.retryAfterSeconds).toBe(45);
    expect(classified.userMessage).toContain('45 second');
  });

  it('still reports a throttle when the server advertises no window', async () => {
    const harness = refusingHarness(429);
    const classified = harness.api.apiFailure(await refusal(harness));
    expect(classified.retryAfterSeconds).toBeUndefined();
    expect(classified.userMessage).toMatch(/too many requests/i);
  });

  it('keeps the 503 outage distinct from the 401 the server separates it from', async () => {
    const harness = refusingHarness(503);
    const classified = harness.api.apiFailure(await refusal(harness));
    expect(classified.status).toBe(503);
    expect(classified.reauthenticate).toBe(false);
    expect(classified.userMessage).not.toMatch(/sign in/i);
  });

  it('classifies nothing for a failure the API did not make deliberately', async () => {
    const harness = refusingHarness(500);
    const error = await refusal(harness);
    expect(harness.api.apiFailure(error)).toBeUndefined();
    expect(harness.api.apiFailureMessage(error, 'Failed to load workbook')).toBe(
      'Failed to load workbook'
    );
  });

  it('carries no response body or header text into the user-facing message', async () => {
    // SECURITY: the message is derived from the status code alone, so a server-side detail
    // cannot reach the interface through this path.
    const harness = refusingHarness(401, { 'X-Internal-Detail': 'psycopg2 OperationalError' });
    const classified = harness.api.apiFailure(await refusal(harness));
    expect(classified.userMessage).not.toContain('psycopg2');
    expect(classified.userMessage).not.toContain('OperationalError');
  });

  it('supplies the classified message to a caller that asks for one', async () => {
    const harness = refusingHarness(401);
    const error = await refusal(harness);
    expect(harness.api.apiFailureMessage(error, 'Failed to load workbook')).toMatch(
      /sign in again/i
    );
  });
});

describe('Development reload hygiene', () => {
  it('ejects both interceptors when the module instance is discarded', () => {
    const harness = loadApiModule({
      token: 'an-id-token',
      withHotModuleReplacement: true,
    });
    expect(harness.disposers).toHaveLength(1);
    expect(harness.client.interceptors.request.__handlers[0]).not.toBeNull();
    expect(harness.client.interceptors.response.__handlers[0]).not.toBeNull();

    harness.disposers[0]();

    expect(harness.client.interceptors.request.__handlers[0]).toBeNull();
    expect(harness.client.interceptors.response.__handlers[0]).toBeNull();
  });

  it('registers no disposer in a production bundle, which has no hot module api', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(harness.disposers).toHaveLength(0);
  });
});

describe('Request and response shapes match the backend contract', () => {
  it('reads the workbook collection from the unprefixed route', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [WORKBOOK_DTO] };
    const workbooks = await harness.api.fetchWorkbooks();
    expect(harness.client.__sent[0].method).toBe('get');
    expect(harness.client.__sent[0].url).toBe('/workbooks');
    expect(workbooks).toEqual([WORKBOOK_DTO]);
  });

  it('returns the collection response with every declared field intact', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: [WORKBOOK_DTO] };
    const [workbook] = await harness.api.fetchWorkbooks();
    expect(Object.keys(workbook).sort()).toEqual([
      'created_at',
      'id',
      'modified_at',
      'name',
      'owner_id',
      'settings',
      'worksheets',
    ]);
    expect(Object.keys(workbook.worksheets[0]).sort()).toEqual([
      'cells',
      'name',
      'named_ranges',
    ]);
    expect(Object.keys(workbook.worksheets[0].cells.A1).sort()).toEqual([
      'formula',
      'style',
      'value',
    ]);
  });

  it('creates a workbook by posting the workbook itself', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: WORKBOOK_DTO };
    const created = await harness.api.createWorkbook(WORKBOOK_DTO);
    expect(harness.client.__sent[0].method).toBe('post');
    expect(harness.client.__sent[0].url).toBe('/workbooks');
    expect(harness.client.__sent[0].data).toEqual(WORKBOOK_DTO);
    expect(created).toEqual(WORKBOOK_DTO);
  });

  it('updates a cell by putting a one-element list to the cells route', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: { message: 'Successfully updated 1 cells' } };
    const result = await harness.api.updateCell('wb-1', 'ws-1', CELL_DTO);
    expect(harness.client.__sent[0].method).toBe('put');
    expect(harness.client.__sent[0].url).toBe('/workbooks/wb-1/worksheets/ws-1/cells');
    expect(harness.client.__sent[0].data).toEqual([CELL_DTO]);
    // The route answers with an acknowledgement, so the accepted cell is returned.
    expect(result).toEqual(CELL_DTO);
  });

  it('coalesces cell writes arriving in one window into a single request', async () => {
    // The window bounds the client's own write rate against the backend `rate_limit_write`
    // budget, so this is the behaviour that keeps ordinary editing clear of a 429.
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: { message: 'Successfully updated 3 cells' } };
    const cells = [
      { value: '1', formula: null, style: {} },
      { value: '2', formula: null, style: {} },
      { value: '3', formula: null, style: {} },
    ];
    const accepted = await Promise.all(
      cells.map((cell) => harness.api.updateCell('wb-1', 'ws-1', cell))
    );
    expect(harness.client.__sent).toHaveLength(1);
    expect(harness.client.__sent[0].data).toEqual(cells);
    expect(accepted).toEqual(cells);
  });

  it('keeps a second worksheet in its own request', async () => {
    // The route addresses one worksheet, so two worksheets are two requests — which is why
    // the write budget has to accommodate several worksheets being edited at once.
    const harness = loadApiModule({ token: 'an-id-token' });
    harness.client.__nextResponse = { data: { message: 'Successfully updated 1 cells' } };
    const first = { value: '1', formula: null, style: {} };
    const second = { value: '2', formula: null, style: {} };
    await Promise.all([
      harness.api.updateCell('wb-1', 'ws-1', first),
      harness.api.updateCell('wb-1', 'ws-2', second),
    ]);
    expect(harness.client.__sent).toHaveLength(2);
    const urls = harness.client.__sent.map((sent: any) => sent.url).sort();
    expect(urls).toEqual([
      '/workbooks/wb-1/worksheets/ws-1/cells',
      '/workbooks/wb-1/worksheets/ws-2/cells',
    ]);
  });

  it('exports exactly the three call sites the application uses', () => {
    // The two diagnostics helpers are deliberately excluded from this comparison and asserted
    // separately below: they issue no request, so counting them here would turn the guard that
    // catches an accidental fourth CALL SITE into one that merely counts exports.
    const harness = loadApiModule({ token: 'an-id-token' });
    const diagnostics = ['apiFailure', 'apiFailureMessage'];
    const callSites = Object.keys(harness.api)
      .filter((name) => !diagnostics.includes(name))
      .sort();
    expect(callSites).toEqual(['createWorkbook', 'fetchWorkbooks', 'updateCell']);
  });

  it('exports the failure classification the pages read, and nothing further', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(Object.keys(harness.api).sort()).toEqual([
      'apiFailure',
      'apiFailureMessage',
      'createWorkbook',
      'fetchWorkbooks',
      'updateCell',
    ]);
  });
});

describe('Cell writes are coalesced per worksheet', () => {
  it('sends cells edited within one window as a single request', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    const first = { value: '1', formula: null, style: {} };
    const second = { value: '2', formula: null, style: {} };
    harness.client.__nextResponse = { data: { message: 'Successfully updated 2 cells' } };

    const settled = await Promise.all([
      harness.api.updateCell('wb-1', 'ws-1', first),
      harness.api.updateCell('wb-1', 'ws-1', second),
    ]);

    expect(harness.client.__sent).toHaveLength(1);
    expect(harness.client.__sent[0].method).toBe('put');
    expect(harness.client.__sent[0].url).toBe('/workbooks/wb-1/worksheets/ws-1/cells');
    expect(harness.client.__sent[0].data).toEqual([first, second]);
    // Every caller is settled with the cell it supplied, in the order it was queued.
    expect(settled).toEqual([first, second]);
  });

  it('never merges writes addressed to different worksheets', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    const onSheetOne = { value: '1', formula: null, style: {} };
    const onSheetTwo = { value: '2', formula: null, style: {} };
    harness.client.__nextResponse = { data: { message: 'Successfully updated 1 cells' } };

    await Promise.all([
      harness.api.updateCell('wb-1', 'ws-1', onSheetOne),
      harness.api.updateCell('wb-1', 'ws-2', onSheetTwo),
    ]);

    expect(harness.client.__sent).toHaveLength(2);
    const byUrl = harness.client.__sent.slice().sort((left: any, right: any) =>
      left.url.localeCompare(right.url)
    );
    expect(byUrl[0].url).toBe('/workbooks/wb-1/worksheets/ws-1/cells');
    expect(byUrl[0].data).toEqual([onSheetOne]);
    expect(byUrl[1].url).toBe('/workbooks/wb-1/worksheets/ws-2/cells');
    expect(byUrl[1].data).toEqual([onSheetTwo]);
  });

  it('rejects every caller in a batch when the request fails', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    const failure: any = new Error('the route refused the batch');
    failure.__isAxiosError = true;
    harness.client.__nextResponse = { error: failure };

    const rejections: unknown[] = [];
    const attempt = (value: string): Promise<void> =>
      harness.api
        .updateCell('wb-1', 'ws-1', { value, formula: null, style: {} })
        .then(
          () => {
            throw new Error('the batch was expected to fail');
          },
          (reason: unknown) => {
            rejections.push(reason);
          }
        );

    await Promise.all([attempt('1'), attempt('2')]);

    expect(harness.client.__sent).toHaveLength(1);
    expect(rejections).toHaveLength(2);
    rejections.forEach((reason) => expect(reason).toBe(failure));
  });
});
