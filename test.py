import streamlit as st
import os
import PIL.Image
from dotenv import load_dotenv
import google.generativeai as genai
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
import time
import json
import re
from googletrans import Translator

################################################################################
# Section 1: Configuration and Initialization
#
# Loads environment variables (Gemini API key), configures Gemini and the translator,
# and initializes the Streamlit page.
################################################################################
load_dotenv()
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(model_name="gemini-2.0-flash")
translator = Translator()

st.set_page_config(page_title="End-to-End Amazon Ad Extractor & Quality Scorer", layout="centered")
st.title("End-to-End Amazon Ad Extractor & Quality Scorer")

url = st.text_input("Enter the URL of the product page", placeholder="https://www.amazon.in/…")

################################################################################
# Section 2: Clean JSON Response Function
#
# Removes markdown code fences and a leading "json" label from the response.
################################################################################
def clean_json_response(text: str) -> str:
    cleaned_text = text.strip("` \n")
    if cleaned_text.lower().startswith("json"):
        cleaned_text = cleaned_text[len("json"):].strip()
    return cleaned_text

################################################################################
# Section 3: Augment Final JSON Using Regex
#
# Fills in missing fields by searching the raw OCR text for known patterns.
# Modifications include:
#  - Always override the availability with "In Stock" if "in stock" is found in the raw text.
#  - If MRP is missing, calculate it using current price and discount (if available).
################################################################################
def augment_final_json(final_json: dict, raw_text: str) -> dict:
    # --- Pricing: Extract MRP if missing or calculate it ---
    if not final_json.get("pricing", {}).get("MRP"):
        mrp_match = re.search(r"M\.R\.P\.?:\s*(₹[\d,\.]+?)(?=\s*M\.R\.P\.?:|$)", raw_text, re.IGNORECASE)
        if mrp_match:
            final_json.setdefault("pricing", {})["MRP"] = mrp_match.group(1).strip()
        else:
            current_price_str = final_json.get("pricing", {}).get("current_price")
            discount_str = final_json.get("pricing", {}).get("discount")
            if current_price_str and discount_str:
                try:
                    discount_percentage = float(re.findall(r'\d+', discount_str)[0]) / 100.0
                    current_price_num = float(current_price_str.replace("₹", "").replace(",", ""))
                    if discount_percentage < 1.0 and discount_percentage > 0:
                        mrp_val = current_price_num / (1 - discount_percentage)
                        final_json.setdefault("pricing", {})["MRP"] = f"₹{mrp_val:,.2f}"
                except Exception:
                    pass

    # --- Pricing: Extract Unit Price ---
    if not final_json.get("pricing", {}).get("unit_price"):
        unit_price_match = re.search(r"([\d₹,\.]+)\s*per\s*(kg|g|L|ml)", raw_text, re.IGNORECASE)
        if unit_price_match:
            final_json.setdefault("pricing", {})["unit_price"] = f"{unit_price_match.group(1).strip()} per {unit_price_match.group(2).strip()}"

    # --- Pricing: Extract Current Price ---
    if not final_json.get("pricing", {}).get("current_price"):
        current_price_match = re.search(r"(₹[\d,\.]+)\s+with", raw_text)
        if current_price_match:
            final_json.setdefault("pricing", {})["current_price"] = current_price_match.group(1).strip()
        else:
            current_price_match = re.search(r"(₹[\d,\.]+)", raw_text)
            if current_price_match:
                final_json.setdefault("pricing", {})["current_price"] = current_price_match.group(1).strip()

    # --- Pricing: Extract Discount ---
    if not final_json.get("pricing", {}).get("discount"):
        discount_match = re.search(r"(-\d+%\s*)", raw_text)
        if discount_match:
            final_json.setdefault("pricing", {})["discount"] = discount_match.group(1).strip()

    # --- Delivery: Availability (Override if "in stock" is found) ---
    if "in stock" in raw_text.lower():
        final_json.setdefault("delivery", {})["availability"] = "In Stock"

    # --- Delivery: Estimated Delivery Time ---
    if not final_json.get("delivery", {}).get("estimated_delivery_time"):
        delivery_time_match = re.search(r"(FREE scheduled delivery[^\n]+)", raw_text, re.IGNORECASE)
        if not delivery_time_match:
            delivery_time_match = re.search(r"(scheduled delivery as soon as[^\n]+)", raw_text, re.IGNORECASE)
        if delivery_time_match:
            final_json.setdefault("delivery", {})["estimated_delivery_time"] = delivery_time_match.group(1).strip()

    # --- Delivery: Shipping Details ---
    if not final_json.get("delivery", {}).get("shipping_details"):
        shipping_details_match = re.search(r"(Delivering to\s+[^\n]+)", raw_text, re.IGNORECASE)
        if shipping_details_match:
            final_json.setdefault("delivery", {})["shipping_details"] = shipping_details_match.group(1).strip()

    # --- Seller: Extract Seller Name ---
    if not final_json.get("seller", {}).get("seller_name"):
        seller_match = re.search(r"Sold by\s*([^\n\r]+)", raw_text, re.IGNORECASE)
        if seller_match:
            final_json.setdefault("seller", {})["seller_name"] = seller_match.group(1).strip()

    # --- Specifications: Extract Weight ---
    if not final_json.get("specifications", {}).get("weight"):
        weight_match = re.search(r"Weight[:\-]?\s*([\d,\.]+\s*(kg|g))", raw_text, re.IGNORECASE)
        if weight_match:
            final_json.setdefault("specifications", {})["weight"] = weight_match.group(1).strip()

    # --- Specifications: Extract Ingredients ---
    if not final_json.get("specifications", {}).get("ingredients"):
        ingredients_match = re.search(r"Ingredients[:\-]?\s*([\w\s,]+)", raw_text, re.IGNORECASE)
        if ingredients_match:
            final_json.setdefault("specifications", {})["ingredients"] = ingredients_match.group(1).strip()

    # --- Reviews: Extract Summary ---
    if not final_json.get("reviews", {}).get("summary"):
        review_match = re.search(r"(\d+(\.\d+)?\s*out of\s*\d+\s*stars)", raw_text, re.IGNORECASE)
        if review_match:
            final_json.setdefault("reviews", {})["summary"] = review_match.group(1).strip()

    return final_json

################################################################################
# Section 4: Full-Page Screenshot Capture
#
# Uses Selenium to load the page, scroll for lazy loading, and capture a full-page screenshot.
################################################################################
def capture_fullpage_screenshot(url):
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.85 Safari/537.36")
    chrome_options.add_argument("--ignore-certificate-errors")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    driver.set_page_load_timeout(60)
    try:
        driver.get(url)
    except Exception as e:
        st.error("Page load timed out. Please check your internet connection or the URL.")
        driver.quit()
        return None
    time.sleep(5)
    
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(2)
    
    scroll_height = driver.execute_script("return document.body.scrollHeight")
    driver.set_window_size(1920, scroll_height)
    time.sleep(2)
    
    screenshot_path = "screenshot.png"
    driver.save_screenshot(screenshot_path)
    driver.quit()
    
    try:
        image = PIL.Image.open(screenshot_path).convert("RGB")
    except Exception as e:
        st.error("Error opening screenshot: " + str(e))
        return None
    return image

################################################################################
# Section 5: Image Segmentation
#
# Splits the captured image into two regions:
# - Top region: Expected to contain the product title, description, and pricing details.
# - Bottom region: Expected to contain delivery, seller info, specifications, and reviews.
################################################################################
def segment_image(image):
    width, height = image.size
    top_region = image.crop((0, 0, width, height // 2))
    bottom_region = image.crop((0, height // 2, width, height))
    return top_region, bottom_region

################################################################################
# Section 6: OCR Extraction on Image Segments
#
# Performs targeted OCR on the top and bottom regions with customized prompts.
################################################################################
def perform_ocr_on_segments(top_region, bottom_region):
    top_prompt = (
        "Extract product title, description, and detailed pricing information (including current price, MRP, discount, and unit price) "
        "from this image region. Look for indicators like '₹', 'MRP:' and discount percentages. Return structured text in JSON format."
    )
    top_response = model.generate_content([top_prompt, top_region])
    top_text = top_response.text

    bottom_prompt = (
        "Extract details about delivery (availability, estimated delivery time, shipping details), seller information "
        "(seller name, shipping origin, fulfillment info), product specifications (weight, dimensions, ingredients), "
        "and a summary of customer reviews and ratings from this image region. Return the output in JSON format."
    )
    bottom_response = model.generate_content([bottom_prompt, bottom_region])
    bottom_text = bottom_response.text

    combined_text = top_text + "\n" + bottom_text
    return combined_text

################################################################################
# Section 7: Reformatting OCR Text into Structured, Buyer-Focused JSON
#
# Uses a refined prompt to convert the raw OCR text into a JSON structure with keys:
# basic_info, pricing, delivery, seller, specifications, and reviews.
################################################################################
def reformat_ocr_text(combined_text):
    buyer_prompt = (
        "Based on the following OCR text extracted from a product page:\n\n" +
        combined_text +
        "\n\nExtract the product information from the perspective of a buyer. "
        "Please return valid JSON with the following keys and use explicit markers where possible:\n\n"
        "1. basic_info: { 'title': <product title>, 'description': <detailed description> }\n"
        "2. pricing: { 'current_price': <current price>, 'MRP': <MRP>, 'discount': <discount>, 'unit_price': <unit price> }\n"
        "3. delivery: { 'availability': <availability>, 'estimated_delivery_time': <estimated delivery time>, 'shipping_details': <shipping details> }\n"
        "4. seller: { 'seller_name': <seller name>, 'shipping_origin': <shipping origin>, 'fulfillment_info': <fulfillment info> }\n"
        "5. specifications: { 'weight': <weight>, 'dimensions': <dimensions>, 'ingredients': <ingredients> }\n"
        "6. reviews: { 'summary': <summary of customer reviews and ratings> }\n\n"
        "Look for explicit markers such as 'MRP:', 'Sold by:', 'Weight:', or 'out of 5 stars'. Return only valid JSON with these keys."
    )
    final_response = model.generate_content([buyer_prompt])
    final_text = final_response.text
    cleaned_text = clean_json_response(final_text)
    try:
        final_json = json.loads(cleaned_text)
    except Exception:
        final_json = {}
    final_json = augment_final_json(final_json, combined_text)
    return final_json

################################################################################
# Section 8: Multi-Language Translation Support
#
# Recursively translates all string fields in a JSON object into English.
################################################################################
def translate_json(obj, dest_language="en"):
    if isinstance(obj, str):
        try:
            translated = translator.translate(obj, dest=dest_language)
            return translated.text
        except Exception:
            return obj
    elif isinstance(obj, dict):
        return {k: translate_json(v, dest_language) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [translate_json(item, dest_language) for item in obj]
    else:
        return obj

################################################################################
# Section 9: Quality Scoring (Automated Moderation)
#
# Computes a quality score based on the completeness of key fields.
################################################################################
def quality_score(final_json):
    score = 0
    if final_json.get("basic_info", {}).get("title"):
        score += 10
    if final_json.get("basic_info", {}).get("description"):
        score += 10
    if final_json.get("pricing", {}).get("current_price"):
        score += 10
    if final_json.get("pricing", {}).get("MRP"):
        score += 5
    if final_json.get("pricing", {}).get("discount"):
        score += 5
    if final_json.get("pricing", {}).get("unit_price"):
        score += 5
    if final_json.get("delivery", {}).get("availability"):
        score += 5
    if final_json.get("delivery", {}).get("estimated_delivery_time"):
        score += 5
    if final_json.get("delivery", {}).get("shipping_details"):
        score += 5
    if final_json.get("seller", {}).get("seller_name"):
        score += 5
    if final_json.get("seller", {}).get("shipping_origin"):
        score += 3
    if final_json.get("seller", {}).get("fulfillment_info"):
        score += 3
    if final_json.get("specifications", {}).get("weight"):
        score += 5
    if final_json.get("specifications", {}).get("dimensions"):
        score += 3
    if final_json.get("specifications", {}).get("ingredients"):
        score += 5
    if final_json.get("reviews", {}).get("summary"):
        score += 10

    final_json["quality_score"] = score
    return final_json

################################################################################
# Section 10: End-to-End Extraction Pipeline
#
# Orchestrates the entire process:
#   1. Capture a full-page screenshot.
#   2. Segment the image.
#   3. Perform OCR on both regions.
#   4. Reformat the raw OCR text into structured JSON.
#   5. Translate the JSON text into English.
#   6. Compute a quality score.
# Returns the final JSON and the raw OCR text.
################################################################################
def extract_product_details(url):
    image = capture_fullpage_screenshot(url)
    if image is None:
        return {"error": "Failed to capture screenshot due to page load timeout."}, ""
    top_region, bottom_region = segment_image(image)
    combined_text = perform_ocr_on_segments(top_region, bottom_region)
    structured_json = reformat_ocr_text(combined_text)
    translated_json = translate_json(structured_json, dest_language="en")
    final_result = quality_score(translated_json)
    return final_result, combined_text

################################################################################
# Section 11: Streamlit Main Interface
#
# The user enters a product URL; the app executes the extraction pipeline and displays
# the final structured JSON (including quality score) and the raw OCR text.
################################################################################
st.subheader("Enter the Product URL to extract details:")
url_input = st.text_input("Product URL", placeholder="https://www.amazon.in/…")

if st.button("Extract Product Details"):
    if url_input:
        with st.spinner("Processing the product page..."):
            final_json, raw_text = extract_product_details(url_input)
        if "error" in final_json:
            st.error(final_json["error"])
        else:
            st.subheader("Final Product Details (Translated, Structured & Quality Scored):")
            st.json(final_json)
            st.subheader("Raw Combined OCR Text:")
            st.code(raw_text)
    else:
        st.warning("Please enter a valid URL.")
