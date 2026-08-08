import { getAuth, signInWithEmailAndPassword, signOut } from 'firebase/auth';
import type { User } from '../schema/workbookTypes';

// HUMAN ASSISTANCE NEEDED
// The following login function may need additional error handling and user data retrieval logic
export async function login(email: string, password: string): Promise<User> {
  try {
    const auth = getAuth();
    const userCredential = await signInWithEmailAndPassword(auth, email, password);
    const { uid, email: signedInEmail, displayName } = userCredential.user;
    return { uid, email: signedInEmail, displayName };
  } catch (error) {
    console.error('Login error:', error);
    throw error;
  }
}

export async function logout(): Promise<void> {
  try {
    const auth = getAuth();
    await signOut(auth);
  } catch (error) {
    console.error('Logout error:', error);
    throw error;
  }
}