import OpenAI from 'openai';
import { config } from 'dotenv';

config();

const GEMINI_API_KEY = process.env.GEMINI_API_KEY;

const client = new OpenAI({
  apiKey: GEMINI_API_KEY,
  baseURL: 'https://generativelanguage.googleapis.com/v1beta/openai/',
});

const DEVANAGARI_RE = /[\u0900-\u097F]/;
const HINDI_KEYWORDS = ['namaste', 'kya', 'hai', 'nahi', 'haan'];

function detectLanguage(text) {
  if (DEVANAGARI_RE.test(text)) return 'hi';
  const lower = text.toLowerCase();
  return HINDI_KEYWORDS.some(kw => lower.includes(kw)) ? 'hi' : 'en';
}

async function askGemini(prompt) {
  if (!GEMINI_API_KEY) {
    return "❌ Set GEMINI_API_KEY in Vercel Environment Variables";
  }

  try {
    const response = await client.chat.completions.create({
      model: 'gemini-2.0-flash-exp',
      messages: [
        {
          role: 'system',
          content: 'You are Jarvis from Iron Man. Answer in 1-2 sentences. Use Hindi (Devanagari script) for Hindi input, English for English. Be concise and helpful.'
        },
        { role: 'user', content: prompt }
      ],
    });
    return response.choices[0].message.content.trim();
  } catch (e) {
    return `❌ AI Error: ${e.message}`;
  }
}

async function processCommand(command) {
  const lower = command.toLowerCase();
  
  // Quick commands
  if (['stop', 'quit', 'cancel'].some(kw => lower.includes(kw))) {
    return { reply: '✅ Speech stopped.', lang: 'en' };
  }
  
  if (lower.includes('open google')) return { reply: '🌐 Opening Google...', lang: 'en' };
  if (lower.includes('open youtube')) return { reply: '📺 Opening YouTube...', lang: 'en' };
  
  // AI Response
  const reply = await askGemini(command);
  const lang = detectLanguage(command);
  return { reply, lang };
}

export default async function handler(req, res) {
  // CORS
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.status(200).end();
    return;
  }

  if (req.method === 'POST') {
    try {
      const { command } = req.body;
      const result = await processCommand(command);
      res.status(200).json(result);
    } catch (e) {
      res.status(500).json({ reply: `Error: ${e.message}`, lang: 'en' });
    }
  } else {
    res.status(200).json({ status: '🚀 Jarvis API Ready!' });
  }
}
