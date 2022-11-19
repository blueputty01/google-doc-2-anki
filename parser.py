import copy
import hashlib
import logging
import os.path
import platform
import re
import shutil
import zipfile
from pathlib import Path

import cssutils
import unicodedata
from bs4 import BeautifulSoup, NavigableString

import anki

INPUT_FILE = None

SYSTEM = platform.system()
if SYSTEM == "Linux":
    INPUT_FILE = Path("/home/alex/desktop/Anki Cards.zip")
else:
    INPUT_FILE = Path("D:/Desktop/Anki Cards.zip")

DECK_NAME_DICT = {
    'APUSH': 'AP US History',
    'Stat': 'AP Stat',
    'Calc': 'Math',
    'Micro': 'AP Micro'
}

CLASS_DICT = {
    'UNDERLINE': 'NOT FOUND',
    'BOLD': 'NOT FOUND',
    'ITALIC': 'NOT FOUND'
}

cssutils.log.setLevel(logging.CRITICAL)

cloze_idx = 1
indents = []
extracted_loc = Path()


def hash_file(path):
    BUFF_SIZE = 65536  # read in 64kb chunks

    sha1 = hashlib.sha1()

    with open(path, 'rb') as f:
        while True:
            data = f.read(BUFF_SIZE)
            if not data:
                break
            sha1.update(data)

    return sha1.hexdigest()


# returns a list of indent utility classes in increasing indent size
def parse_styles(page):
    global indents

    for styles in page.find_all('style'):
        css = cssutils.parseString(styles.encode_contents())
        for rule in css:
            if rule.type == rule.STYLE_RULE:
                style_name = rule.selectorText[1:]  # remove . class selector

                if style_name.startswith('c'):
                    if rule.style.textDecoration == 'underline':
                        CLASS_DICT['UNDERLINE'] = style_name
                    elif rule.style.fontWeight == '700':
                        CLASS_DICT['BOLD'] = style_name
                    elif rule.style.fontStyle == 'italic':
                        CLASS_DICT['ITALIC'] = style_name

                    # look for indentation
                    indent = rule.style.marginLeft
                    if indent == '':
                        # this is not an indent style
                        continue
                    indent = int(re.sub('\\D', '', indent))
                    indents.append({'name': style_name, 'indent': indent})
    indents = sorted(indents, key=lambda x: x['indent'])
    indents = [indent['name'] for indent in indents]


def get_deck_name(tag):
    global DECK_NAME_DICT

    try:
        tag_root = tag.split('::')[0].replace('#', '')
        deck_name = DECK_NAME_DICT[tag_root]
    except AttributeError:
        deck_name = 'Default'
        print('A card was discovered without a deck name. It will be added to the deck called Default')

    return deck_name


def check_extra(ele):
    return hasattr(ele, 'attrs') and 'class' in ele.attrs and not set(ele['class']).isdisjoint(indents)


def parse_element(ele):
    global cloze_idx

    if isinstance(ele, NavigableString):
        return ele, False, None

    extra = check_extra(ele)

    if ele.name in ['span', 'p']:
        text, e, media = parse_list(ele)
        if e:
            extra = True
        if 'class' in ele.attrs:
            if CLASS_DICT['UNDERLINE'] in ele['class']:
                if re.match('^\\d+::', text):
                    local_cloze_idx = int(text.split('::')[0])
                    cloze_idx = local_cloze_idx
                else:
                    text = f'{cloze_idx}::{text}'
                cloze_idx += 1

                end = ''
                while text.endswith(' '):
                    text = text[:-1]
                    end += ' '

                text = f'{{{{c{text}}}}}{end}'
            if CLASS_DICT['BOLD'] in ele['class']:
                text = f'<b>{text}</b>'
            if CLASS_DICT['ITALIC'] in ele['class']:
                text = f'\\({text}\\)'
        if ele.name == 'p':
            text += '\n'
        return text, extra, media

    soup = BeautifulSoup()
    new_tag = soup.new_tag(ele.name)

    if ele.name == "img":
        global extracted_loc

        src_string = ele['src']

        img_path = str(extracted_loc / src_string)
        _, extension = os.path.splitext(src_string)
        image_id = hash_file(img_path)

        filename = image_id + extension

        this_data = {'path': img_path, 'filename': filename}
        media = [this_data]
        new_tag['src'] = f"{filename}"
    else:
        text, e, media = parse_list(ele.children)
        if e:
            extra = True
        new_tag.append(BeautifulSoup(text, features="html.parser"))

    return str(new_tag), extra, media


def parse_list(soup_ele):
    part_list = list(soup_ele)
    if len(part_list) == 0:
        return '', None

    is_single = hasattr(soup_ele, 'name')

    media = []
    parts = []
    extra = False

    for ele in part_list:
        p, e, m = parse_element(ele)
        if e:
            extra = True
        parts.append(p)
        if m is not None and len(m) > 0:
            media.extend(m)
    text = ''.join(parts)
    text = unicodedata.normalize("NFKD", text)
    if is_single and soup_ele.name == 'p':
        text += '\n'
    if check_extra(soup_ele):
        extra = True
    if is_single and soup_ele.name in ['ul', 'ol']:
        text = f'<{soup_ele.name}>{text}</{soup_ele.name}>'
    return text, extra, media


def clean_lists(body):
    global indents

    new_file = BeautifulSoup()
    html_list = None
    level_offset = 0

    def add_html_list():
        nonlocal html_list
        new_file.append(html_list)
        html_list = None
        pass

    for b in body:
        if b.name not in ['ol', 'ul']:
            if html_list is not None:
                add_html_list()
            new_file.append(copy.copy(b))
            continue

        # get level
        first_child = list(b.children)[0]
        current_level = -1
        for c in first_child['class']:
            try:
                local_idx = indents.index(c)
                current_level = local_idx
            except ValueError:
                pass

        if html_list is None:
            level_offset = current_level
            html_list = copy.copy(b)
            continue

        # https://peps.python.org/pep-0448/
        parent = html_list
        nesting = current_level - level_offset
        for idx in range(nesting):
            parent = parent.contents[-1]

        if nesting == 0:
            parent.extend(copy.copy(b))
        else:
            parent.append(copy.copy(b))

    if html_list is not None:
        add_html_list()

    return new_file


def parse_file(soup):
    notes = []

    tag = 'no-tag'

    def new_note():
        global cloze_idx
        notes.append({'text': '', 'extra': '', 'tags': [
                     tag], 'media': [], 'deck': get_deck_name(tag)})
        cloze_idx = 1

    body = soup.find("body").children

    body = clean_lists(body)

    for b in body:
        if b.text.strip() == '':
            if b.find('img') is None:
                # line break
                new_note()
                continue
        if 'title' in b['class']:
            tag = b.text
            new_note()
        else:
            if len(notes) == 0:
                new_note()
            text, extra, m = parse_list(b)
            notes[-1]['extra' if extra else 'text'] += text
            if m is not None and len(m) > 0:
                notes[-1]['media'].extend(m)

    to_return_notes = []
    to_return_media = []
    for note in notes:
        if note['text'] == '':
            continue

        for field in ['text', 'extra']:
            note[field] = note[field].strip().replace('\n', '<br>')

        if '{{c' not in note['text']:
            print('A card was discovered without a cloze deletion.')
            print(note)
            note['text'] += '{{c1::}}'

        rearranged = {'deckName': note['deck'],
                      'modelName': "cloze",
                      'fields': {'Text': note['text'], 'Extra': note['extra']},
                      'tags': note['tags'],
                      "options": {
                          "allowDuplicate": False,
                          "duplicateScope": note['deck'],
                          "duplicateScopeOptions": {
                              "deckName": note['deck'],
                              "checkChildren": False,
                              "checkAllModels": False
                          }
        }}
        to_return_notes.append(rearranged)
        # check if media is already in the list
        for m in note['media']:
            if m not in to_return_media:
                to_return_media.append(m)
    return to_return_notes, to_return_media


def parse():
    global INPUT_FILE, extracted_loc

    extracted_locs = []

    try:
        archive = zipfile.ZipFile(INPUT_FILE, "r")
    except zipfile.BadZipFile:
        print("The file is not a valid zip file.")
        return
    except FileNotFoundError:
        print("The file was not found.")
        return
    file_name = archive.namelist()[0]
    extracted_loc = INPUT_FILE.parents[0] / INPUT_FILE.stem
    archive.extractall(extracted_loc)

    extracted_locs.append(extracted_loc)

    with archive.open(file_name, mode="r") as fp:
        soup = BeautifulSoup(fp, "html.parser")
        if soup.find("br") is not None:
            print("An unexpected <br> element was found. The file may be improperly formatted.")
        parse_styles(soup)
        notes, media = parse_file(soup)

    return notes, media, extracted_locs


if __name__ == "__main__":
    try:
        to_add, to_store_media, to_remove = parse()
    except TypeError:
        print("Nothing was found")
        input("Press enter to exit")
        exit(0)
    print()
    anki.send_notes(to_add)
    anki.send_media(to_store_media)

    for loc in to_remove:
        shutil.rmtree(loc)

    i = input("Delete? (Y/n) ")
    if i == "Y":
        os.remove(INPUT_FILE)
