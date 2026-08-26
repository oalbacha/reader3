"""
Parses an EPUB file into a structured object that can be used to serve the book via a web interface.
"""

import os
import pickle
import re
import shutil
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
from datetime import datetime
from urllib.parse import unquote

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup, Comment

# --- Data structures ---

@dataclass
class ChapterContent:
    """
    Represents a physical file in the EPUB (Spine Item).
    A single file might contain multiple logical chapters (TOC entries).
    """
    id: str           # Internal ID (e.g., 'item_1')
    href: str         # Filename (e.g., 'part01.html')
    title: str        # Best guess title from file
    content: str      # Cleaned HTML with rewritten image paths
    text: str         # Plain text for search/LLM context
    order: int        # Linear reading order


@dataclass
class TOCEntry:
    """Represents a logical entry in the navigation sidebar."""
    title: str
    href: str         # original href (e.g., 'part01.html#chapter1')
    file_href: str    # just the filename (e.g., 'part01.html')
    anchor: str       # just the anchor (e.g., 'chapter1'), empty if none
    children: List['TOCEntry'] = field(default_factory=list)


@dataclass
class BookMetadata:
    """Metadata"""
    title: str
    language: str
    authors: List[str] = field(default_factory=list)
    description: Optional[str] = None
    publisher: Optional[str] = None
    date: Optional[str] = None
    identifiers: List[str] = field(default_factory=list)
    subjects: List[str] = field(default_factory=list)


@dataclass
class Book:
    """The Master Object to be pickled."""
    metadata: BookMetadata
    spine: List[ChapterContent]  # The actual content (linear files)
    toc: List[TOCEntry]          # The navigation tree
    images: Dict[str, str]       # Map: original_path -> local_path

    # Meta info
    source_file: str
    processed_at: str
    version: str = "3.0"
    cover_image: Optional[str] = None  # "images/<name>", set if a cover was detected


# --- Utilities ---

_INLINE_COLOR_RE = re.compile(r'^(color|background(-color)?)\s*:', re.IGNORECASE)


def strip_inline_colors(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove color/background declarations from inline style="" attributes.

    Some EPUBs bake hardcoded colors (e.g. `color: #000000;`) directly into
    chapter HTML. Left alone, these override our theme CSS and can render
    invisible text against a dark background. Other declarations in the same
    style attribute (font-weight, font-size, etc.) are legitimate formatting
    and are kept.
    """
    for tag in soup.find_all(style=True):
        decls = [d.strip() for d in tag['style'].split(';') if d.strip()]
        kept = [d for d in decls if not _INLINE_COLOR_RE.match(d)]
        if kept:
            tag['style'] = '; '.join(kept) + ';'
        else:
            del tag['style']
    return soup


def clean_html_content(soup: BeautifulSoup) -> BeautifulSoup:

    # Remove dangerous/useless tags
    for tag in soup(['script', 'style', 'iframe', 'video', 'nav', 'form', 'button']):
        tag.decompose()

    # Remove HTML comments
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    # Remove input tags
    for tag in soup.find_all('input'):
        tag.decompose()

    soup = strip_inline_colors(soup)

    return soup


def extract_plain_text(soup: BeautifulSoup) -> str:
    """Extract clean text for LLM/Search usage."""
    text = soup.get_text(separator=' ')
    # Collapse whitespace
    return ' '.join(text.split())


def parse_toc_recursive(toc_list, depth=0) -> List[TOCEntry]:
    """
    Recursively parses the TOC structure from ebooklib.
    """
    result = []

    for item in toc_list:
        # ebooklib TOC items are either `Link` objects or tuples (Section, [Children])
        if isinstance(item, tuple):
            section, children = item
            entry = TOCEntry(
                title=section.title,
                href=section.href,
                file_href=section.href.split('#')[0],
                anchor=section.href.split('#')[1] if '#' in section.href else "",
                children=parse_toc_recursive(children, depth + 1)
            )
            result.append(entry)
        elif isinstance(item, epub.Link):
            entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
            result.append(entry)
        # Note: ebooklib sometimes returns direct Section objects without children
        elif isinstance(item, epub.Section):
             entry = TOCEntry(
                title=item.title,
                href=item.href,
                file_href=item.href.split('#')[0],
                anchor=item.href.split('#')[1] if '#' in item.href else ""
            )
             result.append(entry)

    return result


def get_fallback_toc(book_obj) -> List[TOCEntry]:
    """
    If TOC is missing, build a flat one from the Spine.
    """
    toc = []
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            name = item.get_name()
            # Try to guess a title from the content or ID
            title = item.get_name().replace('.html', '').replace('.xhtml', '').replace('_', ' ').title()
            toc.append(TOCEntry(title=title, href=name, file_href=name, anchor=""))
    return toc


def extract_metadata_robust(book_obj) -> BookMetadata:
    """
    Extracts metadata handling both single and list values.
    """
    def get_list(key):
        data = book_obj.get_metadata('DC', key)
        return [x[0] for x in data] if data else []

    def get_one(key):
        data = book_obj.get_metadata('DC', key)
        return data[0][0] if data else None

    return BookMetadata(
        title=get_one('title') or "Untitled",
        language=get_one('language') or "en",
        authors=get_list('creator'),
        description=get_one('description'),
        publisher=get_one('publisher'),
        date=get_one('date'),
        identifiers=get_list('identifier'),
        subjects=get_list('subject')
    )


def _first_img_src(soup: BeautifulSoup) -> Optional[str]:
    """First image reference in a parsed HTML page, checking both plain
    <img src> and the SVG-wrapped <image xlink:href> some EPUBs use for
    fixed-size cover pages."""
    img = soup.find('img')
    if img and img.get('src'):
        return img['src']
    image = soup.find('image')
    if image:
        return image.get('xlink:href') or image.get('href')
    return None


def _resolve_image(src: str, image_map: Dict[str, str]) -> Optional[str]:
    """Look up a (possibly URL-encoded) image src/href in the extracted image map."""
    if not src:
        return None
    src_decoded = unquote(src)
    filename = os.path.basename(src_decoded)
    if src_decoded in image_map:
        return image_map[src_decoded]
    if filename in image_map:
        return image_map[filename]
    return None


def find_cover_image(book_obj, image_map: Dict[str, str]) -> Optional[str]:
    """Best-effort detection of an EPUB's cover image, returning its already-
    extracted local path (a value from `image_map`) or None if nothing matches.

    No single ebooklib API reliably yields a cover across real-world EPUBs, so
    this tries several conventions in order, stopping at the first hit:
      1. EPUB3 manifest cover (properties="cover-image" -> ebooklib.ITEM_COVER)
      2. EPUB2 <meta name="cover" content="ID"/>
      3. The OPF <guide> entry with type="cover" (may point at an image
         directly, or at an HTML title page containing the cover <img>)
      4. An image item whose filename contains "cover"
      5. The first <img> in the first spine document, as a last resort
    """
    # 1. EPUB3 manifest cover
    for item in book_obj.get_items():
        if item.get_type() == ebooklib.ITEM_COVER:
            resolved = _resolve_image(item.get_name(), image_map)
            if resolved:
                return resolved

    # 2. EPUB2 <meta name="cover">
    cover_meta = book_obj.get_metadata('OPF', 'cover')
    if cover_meta:
        _, others = cover_meta[0]
        item = book_obj.get_item_with_id(others.get('content', ''))
        if item:
            resolved = _resolve_image(item.get_name(), image_map)
            if resolved:
                return resolved

    # 3. <guide> entry of type "cover"
    for entry in getattr(book_obj, 'guide', None) or []:
        if entry.get('type') != 'cover':
            continue
        href = entry.get('href', '')
        resolved = _resolve_image(href, image_map)
        if resolved:
            return resolved
        # Might point at an HTML page (e.g. titlepage.xhtml) rather than an image directly.
        item = book_obj.get_item_with_href(href) if hasattr(book_obj, 'get_item_with_href') else None
        if item is None:
            href_name = os.path.basename(unquote(href))
            item = next((i for i in book_obj.get_items() if os.path.basename(i.get_name()) == href_name), None)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content().decode('utf-8', errors='ignore'), 'html.parser')
            src = _first_img_src(soup)
            if src:
                resolved = _resolve_image(src, image_map)
                if resolved:
                    return resolved

    # 4. Filename heuristic
    for item in book_obj.get_items():
        if item.get_type() in (ebooklib.ITEM_IMAGE, ebooklib.ITEM_COVER) and 'cover' in item.get_name().lower():
            resolved = _resolve_image(item.get_name(), image_map)
            if resolved:
                return resolved

    # 5. First image in the first spine document
    if book_obj.spine:
        item_id, _linear = book_obj.spine[0]
        item = book_obj.get_item_with_id(item_id)
        if item and item.get_type() == ebooklib.ITEM_DOCUMENT:
            soup = BeautifulSoup(item.get_content().decode('utf-8', errors='ignore'), 'html.parser')
            src = _first_img_src(soup)
            if src:
                resolved = _resolve_image(src, image_map)
                if resolved:
                    return resolved

    return None


# --- Main Conversion Logic ---

def process_epub(epub_path: str, output_dir: str) -> Book:

    # 1. Load Book
    print(f"Loading {epub_path}...")
    book = epub.read_epub(epub_path)

    # 2. Extract Metadata
    metadata = extract_metadata_robust(book)

    # 3. Prepare Output Directories
    # Only clear the images/ subdirectory (fully regenerated below) --
    # never rmtree the whole output_dir. state.json (reading progress,
    # highlights, notes -- see server.py's load_state/save_state) lives
    # alongside book.pkl in this same directory, and reprocessing an
    # already-read book must not destroy it.
    images_dir = os.path.join(output_dir, 'images')
    if os.path.exists(images_dir):
        shutil.rmtree(images_dir)
    os.makedirs(images_dir, exist_ok=True)

    # 4. Extract Images & Build Map
    print("Extracting images...")
    image_map = {} # Key: internal_path, Value: local_relative_path

    for item in book.get_items():
        # ITEM_COVER is a distinct type ebooklib assigns to EPUB3 manifest
        # covers (properties="cover-image") -- without it, those images
        # were silently dropped from the map even though chapter HTML
        # (e.g. a title page) can still reference them.
        if item.get_type() in (ebooklib.ITEM_IMAGE, ebooklib.ITEM_COVER):
            # Normalize filename
            original_fname = os.path.basename(item.get_name())
            # Sanitize filename for OS
            safe_fname = "".join([c for c in original_fname if c.isalpha() or c.isdigit() or c in '._-']).strip()

            # Save to disk
            local_path = os.path.join(images_dir, safe_fname)
            with open(local_path, 'wb') as f:
                f.write(item.get_content())

            # Map keys: We try both the full internal path and just the basename
            # to be robust against messy HTML src attributes
            rel_path = f"images/{safe_fname}"
            image_map[item.get_name()] = rel_path
            image_map[original_fname] = rel_path

    # 5. Process TOC
    print("Parsing Table of Contents...")
    toc_structure = parse_toc_recursive(book.toc)
    if not toc_structure:
        print("Warning: Empty TOC, building fallback from Spine...")
        toc_structure = get_fallback_toc(book)

    # 6. Process Content (Spine-based to preserve HTML validity)
    print("Processing chapters...")
    spine_chapters = []

    # We iterate over the spine (linear reading order)
    for i, spine_item in enumerate(book.spine):
        item_id, linear = spine_item
        item = book.get_item_with_id(item_id)

        if not item:
            continue

        if item.get_type() == ebooklib.ITEM_DOCUMENT:
            # Raw content
            raw_content = item.get_content().decode('utf-8', errors='ignore')
            soup = BeautifulSoup(raw_content, 'html.parser')

            # A. Fix Images
            for img in soup.find_all('img'):
                src = img.get('src', '')
                if not src: continue

                # Decode URL (part01/image%201.jpg -> part01/image 1.jpg)
                src_decoded = unquote(src)
                filename = os.path.basename(src_decoded)

                # Try to find in map
                if src_decoded in image_map:
                    img['src'] = image_map[src_decoded]
                elif filename in image_map:
                    img['src'] = image_map[filename]

            # B. Clean HTML
            soup = clean_html_content(soup)

            # C. Extract Body Content only
            body = soup.find('body')
            if body:
                # Extract inner HTML of body
                final_html = "".join([str(x) for x in body.contents])
            else:
                final_html = str(soup)

            # D. Create Object
            chapter = ChapterContent(
                id=item_id,
                href=item.get_name(), # Important: This links TOC to Content
                title=f"Section {i+1}", # Fallback, real titles come from TOC
                content=final_html,
                text=extract_plain_text(soup),
                order=i
            )
            spine_chapters.append(chapter)

    # 7. Detect cover image
    print("Detecting cover image...")
    cover_image = find_cover_image(book, image_map)

    # 8. Final Assembly
    final_book = Book(
        metadata=metadata,
        spine=spine_chapters,
        toc=toc_structure,
        images=image_map,
        source_file=os.path.basename(epub_path),
        processed_at=datetime.now().isoformat(),
        cover_image=cover_image
    )

    return final_book


def save_to_pickle(book: Book, output_dir: str):
    p_path = os.path.join(output_dir, 'book.pkl')
    with open(p_path, 'wb') as f:
        pickle.dump(book, f)
    print(f"Saved structured data to {p_path}")


# --- CLI ---

if __name__ == "__main__":

    import sys
    if len(sys.argv) < 2:
        print("Usage: python reader3.py <file.epub>")
        sys.exit(1)

    epub_file = sys.argv[1]
    assert os.path.exists(epub_file), "File not found."
    out_dir = os.path.splitext(epub_file)[0] + "_data"

    book_obj = process_epub(epub_file, out_dir)
    save_to_pickle(book_obj, out_dir)
    print("\n--- Summary ---")
    print(f"Title: {book_obj.metadata.title}")
    print(f"Authors: {', '.join(book_obj.metadata.authors)}")
    print(f"Physical Files (Spine): {len(book_obj.spine)}")
    print(f"TOC Root Items: {len(book_obj.toc)}")
    print(f"Images extracted: {len(book_obj.images)}")
