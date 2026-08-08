/**
 * Verification of the client half of the identity bridge in `api.ts`.
 *
 * The module has import-time side effects: it initialises the Firebase app and registers
 * one request and one response interceptor on its own Axios instance. Each test therefore
 * loads a fresh copy through `loadApiModule`, which transpiles `api.ts` with the declared
 * `typescript` dependency and supplies fakes for `axios`, `firebase/app` and
 * `firebase/auth`, plus a `module` object whose `hot` field is controllable. Nothing here
 * reaches the network, Firebase or the API.
 *
 * Rationale for this loading strategy, and the dependency blocker it works around, are
 * recorded in `documentation/Security Decision Log.md`.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as ts from 'typescript';

const API_BASE_URL = 'https://api.test.example';

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
  __nextResponse: { data?: any; error?: any } | null;
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
      return { status: 200, data: outcome.data, config: resolved, headers: {} };
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

/** Transpile `api.ts` once per test run. */
function apiSource(): string {
  if (transpiledApiSource === null) {
    const source = fs.readFileSync(path.join(__dirname, 'api.ts'), 'utf8');
    transpiledApiSource = ts.transpileModule(source, {
      fileName: 'api.ts',
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2019,
        esModuleInterop: true,
      },
    }).outputText;
  }
  return transpiledApiSource;
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
    await expect(harness.api.createWorkbook({ id: 'wb-1' })).rejects.toThrow(
      'Not authenticated'
    );
    await expect(
      harness.api.updateCell('wb-1', 'ws-1', { value: '1', style: {} })
    ).rejects.toThrow('Not authenticated');
    expect(harness.client.__sent).toHaveLength(0);
  });

  it('rejects when the identity token is an empty string', async () => {
    const harness = loadApiModule({ token: 'unused' });
    harness.setCurrentUser({ getIdToken: () => Promise.resolve('') });
    await expect(harness.api.fetchWorkbooks()).rejects.toThrow('Not authenticated');
    expect(harness.client.__sent).toHaveLength(0);
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
    harness.client.__nextResponse = { data: [{ id: 'wb-1' }] };
    const workbooks = await harness.api.fetchWorkbooks();
    expect(harness.client.__sent[0].method).toBe('get');
    expect(harness.client.__sent[0].url).toBe('/workbooks');
    expect(workbooks).toEqual([{ id: 'wb-1' }]);
  });

  it('creates a workbook by posting the workbook itself', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    const workbook = { id: 'wb-1', name: 'Test Workbook' };
    harness.client.__nextResponse = { data: workbook };
    const created = await harness.api.createWorkbook(workbook);
    expect(harness.client.__sent[0].method).toBe('post');
    expect(harness.client.__sent[0].url).toBe('/workbooks');
    expect(harness.client.__sent[0].data).toEqual(workbook);
    expect(created).toEqual(workbook);
  });

  it('updates a cell by putting a one-element list to the cells route', async () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    const cell = { value: '1', formula: null, style: { bold: 'true' } };
    harness.client.__nextResponse = { data: { message: 'Successfully updated 1 cells' } };
    const result = await harness.api.updateCell('wb-1', 'ws-1', cell);
    expect(harness.client.__sent[0].method).toBe('put');
    expect(harness.client.__sent[0].url).toBe('/workbooks/wb-1/worksheets/ws-1/cells');
    expect(harness.client.__sent[0].data).toEqual([cell]);
    // The route answers with an acknowledgement, so the accepted cell is returned.
    expect(result).toEqual(cell);
  });

  it('exports exactly the three call sites the application uses', () => {
    const harness = loadApiModule({ token: 'an-id-token' });
    expect(Object.keys(harness.api).sort()).toEqual([
      'createWorkbook',
      'fetchWorkbooks',
      'updateCell',
    ]);
  });
});
