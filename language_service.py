from langdetect import detect_langs
from deep_translator import GoogleTranslator

LANG_MAP={"ru":"Russian","de":"German","uk":"Ukrainian","es":"Spanish","fr":"French","he":"Hebrew"}

def detect_language(text):
    text=(text or "").strip()
    if len(text)<12: return None
    try: values=detect_langs(text)
    except Exception: return None
    if not values or values[0].prob<0.85: return None
    code=values[0].lang
    return code if code in LANG_MAP and code!="en" else None

def translate(text,target): return GoogleTranslator(source="auto",target=target).translate(text)
def display_name(code): return LANG_MAP.get(code,code or "")
