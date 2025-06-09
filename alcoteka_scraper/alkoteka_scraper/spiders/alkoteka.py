import scrapy
import json
import os
import re
import time
from html import unescape

from tqdm import tqdm

from ..constants import (
    ALLOWED_DOMAINS,
    CITY_UUID,
    START_URLS,
)


class AlkotekaSpider(scrapy.Spider):
    name = "alkoteka"
    allowed_domains = ALLOWED_DOMAINS   
    city_uuid = CITY_UUID
    per_page = 40

    def __init__(self, urls_file=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if urls_file:
            file_path = os.path.abspath(urls_file)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    self.start_urls = [line.strip() for line in f if line.strip()]
                self.logger.info(f"Загружено {len(self.start_urls)} ссылок из файла: {file_path}")
            except FileNotFoundError:
                self.logger.error(f"Файл не найден: {file_path}")
                self.start_urls = START_URLS
        else:
            self.logger.info("Аргумент urls_file не передан. Используются ссылки по умолчанию.")
            self.start_urls = START_URLS

    def closed(self, reason):
        self.logger.info(f"Парсер завершил работу. Причина: {reason}")

    def parse(self, response):
        slug = response.url.split('/catalog/')[-1].strip('/')
        api_url = f'https://alkoteka.com/web-api/v1/product?city_uuid={self.city_uuid}&root_category_slug={slug}'
        yield scrapy.Request(
            api_url,
            callback=self.parse_total_items,
            meta={'city_uuid': self.city_uuid, 'root_category_slug': slug, }
        )
    
    def parse_total_items(self, response):
        slug = response.meta.get('root_category_slug')
        data = json.loads(response.text)
        meta = data.get("meta", {})
        page = 1
        total_items = meta.get('total')
        api_url = f'https://alkoteka.com/web-api/v1/product?city_uuid={self.city_uuid}&page={page}&per_page={total_items}&root_category_slug={slug}'
        yield scrapy.Request(
            api_url,
            callback=self.parse_api,
            meta={'city_uuid': self.city_uuid, 'root_category_slug': slug, }
        )


    def parse_api(self, response):
        data = json.loads(response.text)
        products = data.get('results', [])

        for product in tqdm(products, desc=f"Обработка продуктов категории {response.meta.get('root_category_slug')}", unit="продукт"):
            slug = product.get('slug')
            self.logger.info(f"{product.get('slug')}")
            if slug:
                detail_api_url = f'https://alkoteka.com/web-api/v1/product/{slug}?city_uuid={self.city_uuid}'
                yield scrapy.Request(
                    detail_api_url,
                    callback=self.parse_product_detail,
                    meta={
                        'slug': slug,
                        'product_url': product.get('product_url')
                        }
                )

    def parse_product_detail(self, response):
        if response.status == 429:
            retry_after = int(response.headers.get('Retry-After', 60))
            self.logger.warning(f"Превышен лимит. Повтор через {retry_after} сек.")
            time.sleep(retry_after)
            yield scrapy.Request(response.url, callback=self.parse_product_detail, dont_filter=True)
        else:
            product = json.loads(response.text)['results']
            product_url = response.meta.get('product_url', '')
            product['product_url'] = product_url
            yield self.format_product_data(product)

    def format_product_data(self, product):
        timestamp = int(time.time())
        title = product.get('name', '')
        self.logger.info(f"{title}")
        color_or_volume = []
        for fl in product.get('filter_labels', []):
            if fl.get('filter') in ('cvet', 'obem'):
                color_or_volume.append(fl.get('title'))
        if color_or_volume:
            title += ", " + ", ".join(color_or_volume)
        current_price_raw = product.get('price', 0)
        try:
            current_price = float(current_price_raw)
        except (TypeError, ValueError):
            current_price = 0.0
        prev_price_raw = product.get('prev_price')
        if prev_price_raw is None: 
            original_price = current_price
        else:
            try:
                original_price = float(prev_price_raw)
            except (TypeError, ValueError):
                original_price = current_price
        sale_tag = ""
        if original_price > current_price and original_price > 0:
            discount_percentage = int(round((original_price - current_price) / original_price * 100))
            sale_tag = f"Скидка {discount_percentage}%"
        in_stock = product.get('available', False)
        count = product.get('quantity_total', 0) if in_stock else 0
        marketing_tags = [tag.get('title', "") for tag in product.get('filter_labels', []) if tag.get('title')]
        brand = ""
        for block in product.get("description_blocks", []):
            if block.get("code") == "brend":
                values = block.get("values", [])
                if values:
                    brand = values[0].get("name", "")
                    break
        section = []
        category = product.get('category', {})
        if category:
            parent = category.get('parent')
            if parent and parent.get('name'):
                section.append(parent.get('name'))
            if category.get('name'):
                section.append(category.get('name'))
        main_image = product.get('image_url')
        set_images = [main_image] if main_image else []
        view360 = []
        video = []
        text_blocks = product.get("text_blocks", [])
        description = ""
        for block in text_blocks:
            if block.get("title") == "Описание":
                html = block.get("content", "")
                html = re.sub(r'<br\s*/?>', '\n', html)
                html = re.sub(r'<[^>]+>', '', html)
                description = unescape(html).strip()
                break
        metadata = {
            "description": description
        }
        for fl in product.get('filter_labels', []):
            key = fl.get('title')
            if key:
                metadata_key = fl.get('filter') or key
                value = fl.get('value') or fl.get('title')
                metadata[metadata_key] = str(value)
        if product.get('vendor_code'):
            metadata['Артикул'] = str(product['vendor_code'])
        variants = 1
        return {
            "timestamp": timestamp,
            "RPC": product.get('uuid'),
            "url": product.get('product_url'),
            "title": title,
            "marketing_tags": marketing_tags,
            "brand": brand,
            "section": section,
            "price_data": {
                "current": current_price,
                "original": original_price,
                "sale_tag": sale_tag,
            },
            "stock": {
                "in_stock": in_stock,
                "count": count,
            },
            "assets": {
                "main_image": main_image,
                "set_images": set_images,
                "view360": view360,
                "video": video,
            },
            "metadata": metadata,
            "variants": variants,
        }
