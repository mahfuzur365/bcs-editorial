/**
 * editorials.js — fetch daily editorials from Firestore and read them aloud.
 *
 * Works in a plain web app AND inside Ionic Capacitor:
 *   - Native (Android/iOS): @capacitor-community/text-to-speech (free, on-device)
 *   - Web/PWA:              Web Speech API (speechSynthesis, free, built-in)
 *
 * Install for Capacitor builds:
 *   npm install @capacitor-community/text-to-speech && npx cap sync
 */

import { initializeApp } from "firebase/app";
import {
  getFirestore, collection, query, where, orderBy, limit, getDocs,
} from "firebase/firestore";
import { Capacitor } from "@capacitor/core";
import { TextToSpeech } from "@capacitor-community/text-to-speech";

// Your web-app config from Firebase console → Project settings → Your apps.
// These values are public identifiers, safe to ship in the client.
const firebaseConfig = {
  apiKey: "YOUR_API_KEY",
  authDomain: "YOUR_PROJECT.firebaseapp.com",
  projectId: "YOUR_PROJECT_ID",
  appId: "YOUR_APP_ID",
};

const app = initializeApp(firebaseConfig);
const db = getFirestore(app); // connects to the (default) database

/* ------------------------------------------------------------------ */
/* Firestore reads                                                     */
/* ------------------------------------------------------------------ */

/** Today's editorials (date is stored as "YYYY-MM-DD" in Bangladesh time). */
export async function getTodayEditorials() {
  const today = new Date(Date.now() + 6 * 3600 * 1000) // shift to UTC+6
    .toISOString().slice(0, 10);
  const q = query(collection(db, "editorials"), where("date", "==", today));
  const snap = await getDocs(q);
  return snap.docs.map((d) => ({ id: d.id, ...d.data() }));
}

/** Most recent N editorials, optionally filtered by category. */
export async function getLatestEditorials(count = 20, category = null) {
  const parts = [collection(db, "editorials")];
  if (category) parts.push(where("category", "==", category));
  parts.push(orderBy("createdAt", "desc"), limit(count));
  const snap = await getDocs(query(...parts));
  return snap.docs.map((d) => ({ id: d.id, ...d.data() }));
}

/* ------------------------------------------------------------------ */
/* Text-to-speech                                                      */
/* ------------------------------------------------------------------ */

const isNative = Capacitor.isNativePlatform();

/**
 * Read an editorial's summary aloud.
 * `editorial.language` is "en" or "bn" (set by the backend pipeline).
 * Note: Bengali ("bn-BD") voices depend on the device — Android usually has
 * Google TTS Bengali; on desktop browsers availability varies.
 */
export async function speakSummary(editorial) {
  const lang = editorial.language === "bn" ? "bn-BD" : "en-US";
  const text = editorial.summary;

  if (isNative) {
    await TextToSpeech.speak({ text, lang, rate: 0.95, pitch: 1.0, volume: 1.0 });
    return;
  }

  // Web Speech API fallback
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = lang;
  const voice = window.speechSynthesis
    .getVoices()
    .find((v) => v.lang.toLowerCase().startsWith(lang.slice(0, 2)));
  if (voice) utterance.voice = voice;
  utterance.rate = 0.95;
  window.speechSynthesis.speak(utterance);
}

/** Read the vocabulary list aloud: English word, then Bengali meaning. */
export async function speakVocabulary(editorial) {
  for (const { word, meaning_bn } of editorial.vocabulary ?? []) {
    if (isNative) {
      await TextToSpeech.speak({ text: word, lang: "en-US", rate: 0.9 });
      await TextToSpeech.speak({ text: meaning_bn, lang: "bn-BD", rate: 0.9 });
    } else {
      await speakOnWeb(word, "en-US");
      await speakOnWeb(meaning_bn, "bn-BD");
    }
  }
}

export async function stopSpeaking() {
  if (isNative) await TextToSpeech.stop();
  else window.speechSynthesis.cancel();
}

function speakOnWeb(text, lang) {
  return new Promise((resolve) => {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = lang;
    u.onend = resolve;
    u.onerror = resolve;
    window.speechSynthesis.speak(u);
  });
}

/* ------------------------------------------------------------------ */
/* Example usage                                                       */
/* ------------------------------------------------------------------ */
// const items = await getTodayEditorials();
// renderList(items);                    // title, source, category, summary
// speakSummary(items[0]);               // 🔊 listen to the summary
// window.open(items[0].pdfUrl);         // 📄 open the full-article PDF
