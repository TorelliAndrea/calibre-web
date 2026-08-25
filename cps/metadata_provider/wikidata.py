# -*- coding: utf-8 -*-

#  This file is part of the Calibre-Web (https://github.com/janeczku/calibre-web)
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program. If not, see <http://www.gnu.org/licenses/>.

# Provider metadati basato su Wikidata + Wikipedia.
#
# Note su dati e licenze (importante):
#   - I dati STRUTTURATI arrivano da Wikidata, che e' rilasciata come CC0
#     (dominio pubblico): autore, editore, data, lingua, genere, ISBN, serie.
#     Nessun obbligo di attribuzione.
#   - La DESCRIZIONE, quando disponibile, viene dall'estratto introduttivo
#     della voce Wikipedia collegata, che e' CC BY-SA: per questo aggiungiamo
#     sempre una riga di attribuzione con licenza e link alla voce.
#   - NON preleviamo copertine: le immagini su Wikipedia/Commons hanno licenze
#     eterogenee (spesso non libere per le copertine) e non sono riusabili
#     senza verifica caso per caso.
#
# API pubbliche, senza chiave:
#   https://www.wikidata.org/w/api.php   (wbsearchentities, wbgetentities)
#   https://<lang>.wikipedia.org/w/api.php (extracts)

from typing import Dict, List, Optional
from urllib.parse import quote

import requests

from cps import logger
from cps.services.Metadata import MetaRecord, MetaSourceInfo, Metadata

log = logger.create()


class Wikidata(Metadata):
    __name__ = "Wikidata"
    __id__ = "wikidata"
    DESCRIPTION = "Wikidata"
    META_URL = "https://www.wikidata.org/"
    ENTITY_URL = "https://www.wikidata.org/wiki/"
    API_URL = "https://www.wikidata.org/w/api.php"

    # Quanti risultati proporre al massimo e per quanti scaricare l'estratto
    # Wikipedia (una richiesta a testa: teniamolo basso).
    MAX_RESULTS = 8
    EXTRACT_LIMIT = 5
    TIMEOUT = (5, 15)  # (connect, read) secondi
    # Wikimedia richiede uno User-Agent descrittivo e rifiuta quello di
    # default di requests (risposta HTML -> JSONDecodeError). Vedi:
    # https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_User-Agent_Policy
    HEADERS = {
        "User-Agent": "Calibre-Web-MetadataProvider/1.0 "
        "(+https://github.com/janeczku/calibre-web)"
    }

    # "instance of" (P31) che identificano un'opera/libro.
    BOOK_TYPES = {
        "Q571",  # book
        "Q7725634",  # literary work
        "Q47461344",  # written work
        "Q3331189",  # version, edition, or translation
        "Q8261",  # novel
        "Q49084",  # short story
        "Q25379",  # play
        "Q5185279",  # poem
        "Q1372064",  # short story collection
        "Q13136",  # reference work
        "Q690851",  # anthology
        "Q1667921",  # novel series
    }

    def search(
        self, query: str, generic_cover: str = "", locale: str = "en"
    ) -> Optional[List[MetaRecord]]:
        if not self.active:
            return []
        lang = self._locale_to_lang(locale)
        try:
            candidate_ids = self._search_entities(query, lang)
            if not candidate_ids:
                return []
            entities = self._get_entities(
                candidate_ids, lang, props="labels|descriptions|claims|sitelinks"
            )
        except Exception as e:
            log.warning("Wikidata search failed: %s", e)
            return []

        # Mantieni l'ordine dei risultati di ricerca e tieni solo i libri.
        books = []
        for qid in candidate_ids:
            entity = entities.get(qid)
            if entity and self._is_book(entity):
                books.append((qid, entity))
            if len(books) >= self.MAX_RESULTS:
                break
        if not books:
            return []

        # Risolvi in un colpo solo le etichette delle entita' referenziate
        # (autori, editore, genere, serie, lingua).
        ref_ids = set()
        for _, entity in books:
            ref_ids.update(self._referenced_ids(entity))
        labels = {}
        if ref_ids:
            try:
                ref_entities = self._get_entities(
                    list(ref_ids), lang, props="labels"
                )
                for rid, ent in ref_entities.items():
                    labels[rid] = self._label_of(ent, lang)
            except Exception as e:
                log.warning("Wikidata label lookup failed: %s", e)

        results = []
        for idx, (qid, entity) in enumerate(books):
            record = self._build_record(
                qid, entity, labels, lang, generic_cover, idx
            )
            if record:
                results.append(record)
        return results

    # ---- Chiamate API --------------------------------------------------

    def _search_entities(self, query: str, lang: str) -> List[str]:
        # Ricerca full-text (CirrusSearch): tollera query tipo "Titolo Autore",
        # mentre wbsearchentities matcha solo l'etichetta esatta. Sugli item
        # (namespace 0) il "title" del risultato E' il QID.
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": 0,
            "srlimit": 15,
            "uselang": lang,
            "format": "json",
        }
        resp = requests.get(
            self.API_URL, params=params, headers=self.HEADERS, timeout=self.TIMEOUT
        )
        resp.raise_for_status()
        ids = [
            item["title"]
            for item in resp.json().get("query", {}).get("search", [])
        ]
        if ids:
            return ids
        return self._search_entities_by_label(query, lang)

    def _search_entities_by_label(self, query: str, lang: str) -> List[str]:
        params = {
            "action": "wbsearchentities",
            "search": query,
            "language": lang,
            "uselang": lang,
            "type": "item",
            "limit": 15,
            "format": "json",
        }
        resp = requests.get(
            self.API_URL, params=params, headers=self.HEADERS, timeout=self.TIMEOUT
        )
        resp.raise_for_status()
        return [item["id"] for item in resp.json().get("search", [])]

    def _get_entities(
        self, ids: List[str], lang: str, props: str
    ) -> Dict[str, Dict]:
        """wbgetentities accetta al massimo 50 id per chiamata."""
        out = {}
        languages = lang + "|en" if lang != "en" else "en"
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            params = {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": props,
                "languages": languages,
                "format": "json",
            }
            resp = requests.get(
                self.API_URL, params=params, headers=self.HEADERS,
                timeout=self.TIMEOUT
            )
            resp.raise_for_status()
            out.update(resp.json().get("entities", {}))
        return out

    def _fetch_extract(self, lang: str, title: str) -> str:
        api = "https://{}.wikipedia.org/w/api.php".format(lang)
        params = {
            "action": "query",
            "prop": "extracts",
            "exintro": 1,
            "explaintext": 1,
            "redirects": 1,
            "titles": title,
            "format": "json",
        }
        resp = requests.get(
            api, params=params, headers=self.HEADERS, timeout=self.TIMEOUT
        )
        resp.raise_for_status()
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract")
            if extract:
                return extract.strip()
        return ""

    # ---- Costruzione record --------------------------------------------

    def _build_record(
        self,
        qid: str,
        entity: Dict,
        labels: Dict[str, str],
        lang: str,
        generic_cover: str,
        idx: int,
    ) -> Optional[MetaRecord]:
        claims = entity.get("claims", {})

        title = self._monolingual(claims, "P1476") or self._label_of(entity, lang)
        if not title:
            return None

        authors = [labels[a] for a in self._entity_ids(claims, "P50") if a in labels]
        authors += self._strings(claims, "P2093")  # autori senza item Wikidata

        record = MetaRecord(
            id=qid,
            title=title,
            authors=authors,
            url=self.ENTITY_URL + qid,
            source=MetaSourceInfo(
                id=self.__id__,
                description=self.DESCRIPTION,
                link=self.META_URL,
            ),
        )

        record.cover = generic_cover  # nessuna copertina da Wikidata (vedi note)
        record.publisher = self._first_label(claims, "P123", labels)
        record.publishedDate = self._date(claims, "P577")
        record.languages = self._labels_list(claims, "P407", labels)
        record.tags = self._labels_list(claims, "P136", labels)  # genere
        record.series, record.series_index = self._series(claims, labels)
        record.identifiers = self._identifiers(qid, claims)
        record.description = self._description(entity, claims, lang, idx)
        return record

    def _description(
        self, entity: Dict, claims: Dict, lang: str, idx: int
    ) -> str:
        # Estratto Wikipedia (CC BY-SA) con attribuzione, solo per i primi
        # risultati per limitare le richieste; altrimenti la breve descrizione
        # Wikidata (CC0).
        if idx < self.EXTRACT_LIMIT:
            sitelinks = entity.get("sitelinks", {})
            for wiki in (lang + "wiki", "enwiki"):
                link = sitelinks.get(wiki)
                if not link:
                    continue
                wlang = wiki[:-4]
                try:
                    extract = self._fetch_extract(wlang, link["title"])
                except Exception as e:
                    log.warning("Wikipedia extract failed: %s", e)
                    extract = ""
                if extract:
                    url = "https://{}.wikipedia.org/wiki/{}".format(
                        wlang, quote(link["title"].replace(" ", "_"))
                    )
                    attribution = (
                        '\n\n— {}, Wikipedia (CC BY-SA 4.0): {}'.format(
                            link["title"], url
                        )
                    )
                    return extract + attribution
        desc = entity.get("descriptions", {})
        return (desc.get(lang) or desc.get("en") or {}).get("value", "")

    # ---- Helper sui claim Wikidata -------------------------------------

    @staticmethod
    def _is_book(entity: Dict) -> bool:
        claims = entity.get("claims", {})
        for qid in Wikidata._entity_ids(claims, "P31"):
            if qid in Wikidata.BOOK_TYPES:
                return True
        # Ripiego: ha un autore -> molto probabilmente un'opera scritta.
        return bool(claims.get("P50") or claims.get("P2093"))

    @staticmethod
    def _referenced_ids(entity: Dict) -> set:
        claims = entity.get("claims", {})
        ids = set()
        for prop in ("P50", "P123", "P136", "P407", "P179"):
            ids.update(Wikidata._entity_ids(claims, prop))
        return ids

    @staticmethod
    def _entity_ids(claims: Dict, prop: str) -> List[str]:
        out = []
        for statement in claims.get(prop, []):
            value = Wikidata._value(statement)
            if isinstance(value, dict) and "id" in value:
                out.append(value["id"])
        return out

    @staticmethod
    def _strings(claims: Dict, prop: str) -> List[str]:
        out = []
        for statement in claims.get(prop, []):
            value = Wikidata._value(statement)
            if isinstance(value, str):
                out.append(value)
        return out

    @staticmethod
    def _monolingual(claims: Dict, prop: str) -> str:
        for statement in claims.get(prop, []):
            value = Wikidata._value(statement)
            if isinstance(value, dict) and value.get("text"):
                return value["text"]
        return ""

    @staticmethod
    def _first_label(claims: Dict, prop: str, labels: Dict[str, str]) -> Optional[str]:
        for qid in Wikidata._entity_ids(claims, prop):
            if labels.get(qid):
                return labels[qid]
        return None

    @staticmethod
    def _labels_list(claims: Dict, prop: str, labels: Dict[str, str]) -> List[str]:
        return [labels[q] for q in Wikidata._entity_ids(claims, prop) if labels.get(q)]

    @staticmethod
    def _series(claims: Dict, labels: Dict[str, str]):
        for statement in claims.get("P179", []):
            value = Wikidata._value(statement)
            if isinstance(value, dict) and labels.get(value.get("id")):
                index = 1
                ordinal = statement.get("qualifiers", {}).get("P1545")
                if ordinal:
                    raw = Wikidata._snak_value(ordinal[0])
                    try:
                        index = float(raw)
                        index = int(index) if index.is_integer() else index
                    except (TypeError, ValueError):
                        index = 1
                return labels[value["id"]], index
        return "", 1

    @staticmethod
    def _identifiers(qid: str, claims: Dict) -> Dict[str, str]:
        identifiers = {"wikidata": qid}
        isbn13 = Wikidata._strings(claims, "P212")
        isbn10 = Wikidata._strings(claims, "P957")
        if isbn13:
            identifiers["isbn"] = isbn13[0].replace("-", "")
        elif isbn10:
            identifiers["isbn"] = isbn10[0].replace("-", "")
        return identifiers

    @staticmethod
    def _date(claims: Dict, prop: str) -> str:
        for statement in claims.get(prop, []):
            value = Wikidata._value(statement)
            if isinstance(value, dict) and value.get("time"):
                # Formato: "+1937-00-00T00:00:00Z" (mese/giorno 00 se ignoti).
                raw = value["time"].lstrip("+")
                date_part = raw.split("T")[0]
                pieces = date_part.split("-")
                if len(pieces) == 3:
                    year, month, day = pieces
                    month = "01" if month == "00" else month
                    day = "01" if day == "00" else day
                    return "{}-{}-{}".format(year, month, day)
        return ""

    @staticmethod
    def _value(statement: Dict):
        try:
            return statement["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _snak_value(snak: Dict):
        try:
            return snak["datavalue"]["value"]
        except (KeyError, TypeError):
            return None

    @staticmethod
    def _label_of(entity: Dict, lang: str) -> str:
        labels = entity.get("labels", {})
        entry = labels.get(lang) or labels.get("en")
        return entry.get("value", "") if entry else ""

    @staticmethod
    def _locale_to_lang(locale) -> str:
        lang = str(locale) if locale else "en"
        lang = lang.replace("_", "-").split("-")[0].lower()
        return lang or "en"
