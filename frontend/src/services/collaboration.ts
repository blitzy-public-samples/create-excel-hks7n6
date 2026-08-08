import { getFirestore, collection, onSnapshot } from 'firebase/firestore';
import { WorkbookSchema } from 'backend/app/schema/workbook_schema';

// HUMAN ASSISTANCE NEEDED
// The following function needs review and potential modifications for production readiness.
// The confidence level is below 0.8, indicating potential issues or incomplete implementation.
export function subscribeToWorkbookChanges(workbookId: string, callback: (data: WorkbookSchema) => void): () => void {
  const db = getFirestore();
  const workbookRef = collection(db, 'workbooks', workbookId);

  const unsubscribe = onSnapshot(workbookRef, (snapshot) => {
    if (snapshot.exists()) {
      const data = snapshot.data() as WorkbookSchema;
      callback(data);
    } else {
      console.error(`Workbook with ID ${workbookId} not found`);
      // Consider handling this case more gracefully, perhaps by notifying the user or taking alternative action
    }
  }, (error) => {
    console.error('Error listening to workbook changes:', error);
    // Consider implementing a more robust error handling strategy
  });

  return unsubscribe;
}

// Additional comments:
// 1. Error handling: The current implementation logs errors to the console. Consider implementing a more robust error handling strategy, such as notifying the user or retrying the connection.
// 2. Type safety: Ensure that the WorkbookSchema type accurately represents the data structure in Firestore. You may need to add runtime type checking or validation.
// 3. Performance: For large workbooks, consider implementing pagination or limiting the amount of data fetched in real-time.
// 4. Security: Ensure that proper security rules are in place in Firestore to prevent unauthorized access to workbook data.
// 5. Offline support: Consider implementing offline support and data persistence for a better user experience.