import { getAuth, signInWithEmailAndPassword, signOut } from 'firebase/auth';
import { UserSchema } from 'backend/app/schema/workbook_schema';

// HUMAN ASSISTANCE NEEDED
// The following login function may need additional error handling and user data retrieval logic
export async function login(email: string, password: string): Promise<UserSchema> {
  try {
    const auth = getAuth();
    const userCredential = await signInWithEmailAndPassword(auth, email, password);
    // TODO: Fetch additional user data if needed to match UserSchema
    return userCredential.user as unknown as UserSchema;
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