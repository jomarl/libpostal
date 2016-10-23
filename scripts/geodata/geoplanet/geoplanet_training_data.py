import argparse
import csv
import itertools
import os
import six
import sqlite3
import sys

from collections import defaultdict

this_dir = os.path.realpath(os.path.dirname(__file__))
sys.path.append(os.path.realpath(os.path.join(os.pardir, os.pardir)))

from geodata.address_expansions.abbreviations import abbreviate
from geodata.address_expansions.equivalence import equivalent
from geodata.address_expansions.gazetteers import *

from geodata.address_formatting.formatter import AddressFormatter

from geodata.countries.names import country_names
from geodata.names.normalization import name_affixes
from geodata.places.config import place_config

from geodata.csv_utils import tsv_string, unicode_csv_reader

GEOPLANET_DB_FILE = 'geoplanet.db'
GEOPLANET_FORMAT_DATA_TAGGED_FILENAME = 'geoplanet_formatted_addresses_tagged.tsv'
GEOPLANET_FORMAT_DATA_FILENAME = 'geoplanet_formatted_addresses.tsv'


class GeoPlanetFormatter(object):
    # Map of GeoPlanet language codes to ISO-639 alpha2 language codes
    language_codes = {
        'ENG': 'en',
        'JPN': 'ja',
        'GER': 'de',
        'SPA': 'es',
        'FRE': 'fr',
        'UNK': 'unk',
        'ITA': 'it',
        'POR': 'pt',
        'POL': 'pl',
        'ARA': 'ar',
        'CZE': 'cs',
        'SWE': 'sv',
        'CHI': 'zh',
        'RUM': 'ro',
        'FIN': 'fi',
        'DUT': 'nl',
        'NOR': 'nb',
        'DAN': 'da',
        'HUN': 'hu',
        'KOR': 'kr',
    }

    non_latin_script_languages = {
        'JPN',  # Japanese
        'ARA',  # Arabic
        'CHI',  # Chinese
        'KOR',  # Korean
    }

    ALIAS_PREFERRED = 'P'
    ALIAS_PREFERRED_FOREIGN = 'Q'
    ALIAS_VARIANT = 'V'
    ALIAS_ABBREVIATED = 'A'
    ALIAS_COLLOQUIAL = 'S'

    # Map of GeoPlanet place types to address formatter types
    place_types = {
        'Continent': AddressFormatter.WORLD_REGION,
        'Country': AddressFormatter.COUNTRY,
        'CountryRegion': AddressFormatter.COUNTRY_REGION,
        'State': AddressFormatter.STATE,
        'County': AddressFormatter.STATE_DISTRICT,
        'Island': AddressFormatter.ISLAND,
        'Town': AddressFormatter.CITY,
        # Note: if we do general place queris from GeoPlanet, this
        # may have to be mapped more carefully
        'LocalAdmin': AddressFormatter.CITY_DISTRICT,
        'Suburb': AddressFormatter.SUBURB,
    }

    def __init__(self, geoplanet_db):
        self.db = sqlite3.connect(geoplanet_db)

        # These aren't too large and it's easier to have them in memory
        self.places = {row[0]: row[1:] for row in self.db.execute('select * from places')}
        self.aliases = defaultdict(list)

        print('Doing aliases')
        for row in self.db.execute('''select a.* from aliases a
                                      left join places p
                                          on a.id = p.id
                                          and p.place_type in ("State", "County")
                                          and a.language != p.language
                                      where name_type != "S" -- no colloquial aliases like "The Big Apple"
                                      and name_type != "V" -- variants can often be demonyms like "Welsh" or "English" for UK
                                      and p.id is NULL -- exclude foreign-language states/county names
                                      order by id, language,
                                      case name_type
                                          when "P" then 1
                                          when "Q" then 2
                                          when "V" then 3
                                          when "A" then 4
                                          when "S" then 5
                                          else 6
                                      end'''):
            place = self.places.get(row[0])
            if not place:
                continue

            self.aliases[row[0]].append(row[1:])

        print('Doing variant aliases')
        variant_aliases = 0
        for i, row in enumerate(self.db.execute('''select a.*, p.name from aliases a
                                                   join places p using(id)
                                                   where a.name_type = "V"
                                                   and a.language = p.language''')):
            place_name = row[-1]

            row = row[:-1]
            place_id, alias, name_type, language = row

            language = self.language_codes[language]
            if language != 'unk':
                alias_sans_affixes = name_affixes.replace_prefixes(name_affixes.replace_suffixes(alias, language), language)
                if alias_sans_affixes:
                    alias = alias_sans_affixes

                place_name_sans_affixes = name_affixes.replace_prefixes(name_affixes.replace_suffixes(place_name, language), language)
                if place_name_sans_affixes:
                    place_name = place_name_sans_affixes
            else:
                language = None

            if equivalent(place_name, alias, toponym_abbreviations_gazetteer, language):
                self.aliases[row[0]].append(row[1:])
                variant_aliases += 1

            if i % 10000 == 0 and i > 0:
                print('tested {} variant aliases with {} positives'.format(i, variant_aliases))

        self.aliases = dict(self.aliases)

        self.formatter = AddressFormatter()

    def get_place_hierarchy(self, place_id):
        all_places = []
        original_place_id = place_id
        place = self.places[place_id]
        all_places.append((place_id, ) + place)
        place_id = place[-1]
        while place_id != 1 and place_id != original_place_id:
            place = self.places[place_id]
            all_places.append((place_id,) + place)
            place_id = place[-1]
        return all_places

    def get_aliases(self, place_id):
        return self.aliases.get(place_id, [])

    def cleanup_name(self, name):
        return name.strip(' ,-')

    def format_postal_codes(self, tag_components=True):
        all_postal_codes = self.db.execute('select * from postal_codes')
        for postal_code_id, country, postal_code, language, place_type, parent_id in all_postal_codes:
            country = country.lower()
            postcode_language = language

            language = self.language_codes[language]

            place_hierarchy = self.get_place_hierarchy(parent_id)

            containing_places = defaultdict(set)

            language_places = {None: containing_places}

            original_language = language

            have_default_language = False

            for place_id, country, name, lang, place_type, parent in place_hierarchy:
                country = country.lower()

                # First language
                if not have_default_language and lang != postcode_language:
                    language = self.language_codes[lang]
                    have_default_language = True

                name = self.cleanup_name(name)

                place_type = self.place_types[place_type]
                containing_places[place_type].add(name)

                if place_type == AddressFormatter.COUNTRY:
                    pass

                aliases = self.get_aliases(place_id)
                for name, name_type, alias_lang in aliases:
                    if not alias_lang:
                        alias_lang = 'UNK'
                    if alias_lang == lang and lang != 'UNK':
                        alias_language = None
                    else:
                        alias_language = self.language_codes[alias_lang]

                    language_places.setdefault(alias_language, defaultdict(set))
                    lang_places = language_places[alias_language]

                    name = self.cleanup_name(name)

                    lang_places[place_type].add(name)

            for language, containing_places in six.iteritems(language_places):
                if language is None:
                    language = original_language

                country_localized_name = country_names.localized_name(country, language)
                if country_localized_name:
                    containing_places[AddressFormatter.COUNTRY].add(country_localized_name)
                country_alpha3_code = country_names.alpha3_code(country)
                if country_alpha3_code and language in (None, 'ENG'):
                    containing_places[AddressFormatter.COUNTRY].add(country_alpha3_code)

                keys = containing_places.keys()
                all_values = containing_places.values()

                for i, values in enumerate(itertools.product(*all_values)):
                    components = {
                        AddressFormatter.POSTCODE: postal_code
                    }

                    components.update(zip(keys, values))

                    format_language = language if self.formatter.template_language_matters(country, language) else None
                    formatted = self.formatter.format_address(components, country, language=format_language,
                                                              minimal_only=False, tag_components=tag_components)

                    yield (language, country, formatted)

                    component_keys = set(components)
                    components = place_config.dropout_components(components, (), country=country, population=0)

                    if len(components) > 1 and set(components) ^ component_keys:
                        formatted = self.formatter.format_address(components, country, language=format_language,
                                                                  minimal_only=False, tag_components=tag_components)
                        yield (language, country, formatted)

    def build_training_data(self, out_dir, tag_components=True):
        if tag_components:
            formatted_tagged_file = open(os.path.join(out_dir, GEOPLANET_FORMAT_DATA_TAGGED_FILENAME), 'w')
            writer = csv.writer(formatted_tagged_file, 'tsv_no_quote')
        else:
            formatted_tagged_file = open(os.path.join(out_dir, GEOPLANET_FORMAT_DATA_FILENAME), 'w')
            writer = csv.writer(formatted_tagged_file, 'tsv_no_quote')

        i = 0

        for language, country, formatted_address in self.format_postal_codes(tag_components=tag_components):
            if not formatted_address or not formatted_address.strip():
                continue

            formatted_address = tsv_string(formatted_address)
            if not formatted_address or not formatted_address.strip():
                continue

            if tag_components:
                row = (language, country, formatted_address)
            else:
                row = (formatted_address,)

            writer.writerow(row)
            i += 1
            if i % 1000 == 0 and i > 0:
                print('did {} formatted addresses'.format(i))


if __name__ == '__main__':
    if len(sys.argv) < 3:
        sys.exit('Usage: python download_geoplanet.py geoplanet_db_path out_dir')

    geoplanet_db_path = sys.argv[1]
    out_dir = sys.argv[2]

    geoplanet = GeoPlanetFormatter(geoplanet_db_path)
    geoplanet.build_training_data(out_dir)