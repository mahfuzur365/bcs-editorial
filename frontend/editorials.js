/**
 * editorials.js — data layer for the editorial screen.
 *
 * UI model this supports:
 *   [ National | International ]   ← toggle (origin field)
 *     ▸ Economy                    ← sections grouped by category
 *         · article card (title, source, summary, 🔊 TTS, 📄 PDF)
 *     ▸ Geopolitics …
 *   ─────────────────────────────
 *   Archive (this month)           ← weekly folders → day → same view
 *     ▸ Week 1  (1–7)
 *         · 2026-07-01 …
 *
 * Retention is handled by the backend: only the current calendar month
 * exists in Firestore, so the archive never needs client-side pruning.
 *
 * All queries use equality filters only → NO composite indexes needed.
 * Sorting is done client-side.
 *
 * TTS: @capacitor-community/text-to-speech on device, Web Speech API on web.
 *   npm install @capacitor-community/text-to-speech && npx cap sync
 */

import { initializeApp } from "firebase/app";
import {
  getFirestore, collection, doc, getDoc, query, where, getDocs,
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

/** Category display order (matches the pipeline's category names exactly). */
export const CATEGORIES = [
  "Economy", "World Politics", "Geopolitics", "War & Peace",
  "Environment", "Agriculture", "Public Policy", "Art, Literature & Culture",
];

/** Today's date the same way the backend defines it (Bangladesh time). */
export function todayDhaka() {
  return new Date(Date.now() + 6 * 3600 * 1000).toISOString().slice(0, 10);
}

/* ------------------------------------------------------------------ */
/* Firestore reads                                                     */
/* ------------------------------------------------------------------ */

/**
 * Articles for one day + one toggle side, newest first.
 * @param {"national"|"international"} origin
 * @param {string} date  "YYYY-MM-DD"; defaults to today
 */
export async function getEditorials(origin, date = todayDhaka()) {
  const q = query(
    collection(db, "editorials"),
    where("origin", "==", origin),
    where("date", "==", date),
  );
  const snap = await getDocs(q);
  return snap.docs
    .map((d) => ({ id: d.id, ...d.data() }))
    .sort((a, b) => (b.createdAt?.seconds ?? 0) - (a.createdAt?.seconds ?? 0));
}

/** Group a result of getEditorials() into { category: [articles] },
 *  ordered like CATEGORIES. Empty categories are omitted. */
export function groupByCategory(articles) {
  const grouped = {};
  for (const cat of CATEGORIES) {
    const items = articles.filter((a) => a.category === cat);
    if (items.length) grouped[cat] = items;
  }
  // Anything with an unexpected category label still gets shown.
  const known = new Set(CATEGORIES);
  const rest = articles.filter((a) => !known.has(a.category));
  if (rest.length) grouped["Others"] = rest;
  return grouped;
}

/**
 * Archive index: one document read. Returns this month's days (newest
 * first) grouped into weekly folders:
 *   [{ week: 1, label: "Week 1", days: ["2026-07-07", …] }, …]
 * Excludes `today` so the archive only shows previous days.
 */
export async function getArchive() {
  const snap = await getDoc(doc(db, "editorial_meta", "days"));
  const today = todayDhaka();
  const dates = ((snap.exists() ? snap.data().dates : []) || [])
    .filter((d) => d < today)
    .sort()
    .reverse();

  const weeks = new Map();
  for (const date of dates) {
    const dayOfMonth = Number(date.slice(8, 10));
    const week = Math.floor((dayOfMonth - 1) / 7) + 1; // 1–7 → wk1, 8–14 → wk2 …
    if (!weeks.has(week)) weeks.set(week, []);
    weeks.get(week).push(date);
  }
  return [...weeks.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([week, days]) => ({ week, label: `Week ${week}`, days }));
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
/* Example: wiring the whole screen                                    */
/* ------------------------------------------------------------------ */
// let origin = "national";                       // toggle state
//
// // Main view (today, grouped by category):
// const grouped = groupByCategory(await getEditorials(origin));
// // → render an accordion section per key; cards show title/source/summary,
// //   a 🔊 button (speakSummary), 📖 vocab chips, and a PDF link (pdfUrl).
//
// // Archive at the bottom (previous days of this month, weekly folders):
// const archive = await getArchive();
// // → [{label: "Week 2", days: ["2026-07-13", "2026-07-12", …]}, …]
// // Tapping a day re-renders the same categorized view for that date:
// const dayView = groupByCategory(await getEditorials(origin, "2026-07-12"));
