#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, 51 Franklin Street, Fifth Floor, Boston, MA 02110-1335, USA.
#
# Authors:
#     Ahmed Zerouali <mr.hmed@gmail.com>
#     Valerio Cosentino <valcos@bitergia.com>
#
import os
import subprocess
import pandas as pd
import warnings
import json as js
import codecs
import apt_pkg
import psycopg2
import requests
apt_pkg.init_system()
from conpan.errors import ParamsError
warnings.simplefilter(action='ignore', category=Warning)

VULS_JSON = 'vulnerabilities.json'
PACKAGES = 'packages.csv'
VULS_CSV = 'vulnerabilities.csv'
BUGS_CSV = 'bugs.csv'

removed = False


class Debian:
    """Debian, a backend to analyze Debian packages

    :param docker: targets the Docker image
    """

    def __init__(self, image=None, data_dir=None):
        self.image = image
        self.data_dir = data_dir
        self.file = image.replace('/', '_')
        self.debian_version = ''
        self.container_dpkg = ''

    def read_csv(self, CSV):
        """Reads a csv file
        :param CSV: the csv file to read
        :return df: dataframe of the csv file
        """
        df = pd.read_csv(self.data_dir + CSV, sep=';', dtype=object, index_col=None, error_bad_lines=False)
        df.drop_duplicates(inplace=True)

        return df

    def parse_json_vuls(self):
        """Loads a JSON file
        :return json: json object
        """

        json = js.load(codecs.open(self.data_dir + VULS_JSON, 'r', 'utf-8'))
        return json

    def command_system(self, cmd):
        """Excutes a Shell command
        :return result: the output of the command
        """

        result = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
        result = list(filter(lambda x: len(x) > 0, (line.strip().decode('utf-8') for line in result.stdout)))

        return result

    def download(self):
        """Pulls and runs the Docker images
        """
        cmd = "docker run --entrypoint '/bin/bash' " + self.image + " -c 'cat /etc/debian_version' "
        self.debian_version = self.command_system(cmd)

        cmd = "docker run --entrypoint '/bin/bash' " + self.image + " -c 'dpkg -l' "
        self.container_dpkg = self.command_system(cmd)

        if removed:
            os.system("docker stop $(docker ps -a | grep '" + self.image + " ' | cut -d' ' -f1)")
            os.system("docker rm $(docker ps -a | grep '" + self.image + " ' | cut -d' ' -f1)")

    @property
    def parse_release(self):
        """Returns the Debian release used in the Container
        """
        try:
            return self.debian_version[0]
        except:
            return ''

    def parse_debian(self):
        """Extracts installed packages in the Docker container
        :return data: a dataframe with the installed packages
        """

        columns = ['name', 'package', 'version']
        data = pd.DataFrame(columns=columns)

        packages = []
        versions = []
        for line in self.container_dpkg:
            if not str(line).startswith('ii'):
                continue
            line = line.split(' ')
            line = sorted(set(line), key=lambda x: line.index(x))
            packages.append(line[2])
            versions.append(line[3])

        df = pd.DataFrame({'name': self.file, 'package': packages, 'version': versions})
        data = data.append(df)
        data.set_index('name', inplace=True)

        data['release_number'] = self.parse_release

        data['debian'] = data['release_number'].apply(lambda x: 'jessie' if x.startswith('8')
                                                                          else 'stretch' if x.startswith('9')
                                                                          else 'wheezy' if x.startswith('7')
                                                                          else 'squeeze' if x.startswith('6')
                                                                          else 'buster' if x.startswith('buster')
                                                                          else x)
        data['package'] = data['package'].apply(lambda x: x.split(':')[0])

        data.drop_duplicates(inplace=True)

        return data

    def track_packages(self, installed_packages):
        """Tracks installed packages to check if they are coming from Debian.
        :param installed_packages: packages found installed in the Container
        :return tracked_packages: packages found installed in the Container and coming from Debian.
        """

        debian_p = self.read_csv(PACKAGES)

        tracked_packages = (installed_packages.
                            set_index(['package', 'version']).
                            merge(debian_p.
                                  set_index(['package', 'version']),
                                  left_index=True,
                                  right_index=True,
                                  how='left')
                            ).reset_index().dropna()
        for column in ['last_order', 'version_order']:
            tracked_packages[column] = tracked_packages[column].apply(int)
        tracked_packages['outdate'] = tracked_packages['last_order'] - tracked_packages['version_order']
        return tracked_packages


    def dates_release_debian(self):
        """many packages are seen in different releases of Debian: We choose the first release where a package version was seen as the Debian release
        :return dict_date: a dictionary with packages and their release date
        :return dict_release: a dictionary with packages and their Debian release version.
        """

        debian_p = self.read_csv(PACKAGES)
        df_packages = (debian_p.
                       sort_values('date', ascending=True).
                       groupby(['source', 'source_version', 'release_snapshot']).
                       first().
                       drop(['package', 'version'], axis=1)
                       )

        dict_date = df_packages.to_dict()  # dict of source version dates

        df_packages_release = (debian_p.
                               sort_values('date', ascending=True).
                               groupby(['source', 'source_version']).
                               first().
                               drop(['package', 'version', 'date'], axis=1)
                               )

        dict_release = df_packages_release.to_dict()  # dict of releases
        return dict_date, dict_release


    def unique_installed_packages(self,tracked_packages):
        """Identify unique source packages.
        A source package may have many binary packages.
        :param tracked_packages: packages found installed in the container
        :return df: a dataframe with unique source packages.
        """

        df = (tracked_packages.  # We create a DF with source packages found in Docker containers
              groupby(['source', 'source_version']).
              count().
              drop(['package', 'version'], axis=1).
              reset_index()
              )  # only source versions and distinct.

        return df


    def final_vuls(self, tracked_packages):
        """Extract knows vulnerabilities from the Debian Security Tracker, for all packages found in the container.
        We consider versions where the vulnerability was fixed as the stop point.
        It extracts bugs for all versions and save them in a file.
        :param tracked_packages: packages found installed in the container
        :return
        """

        vulnerabilities = self.parse_json_vuls()

        dict_date, dict_release = self.dates_release_debian()

        sorted_ip = self.unique_installed_packages(tracked_packages)

        fcsv = open(self.data_dir + VULS_CSV, 'w')
        fcsv.write('source;source_version;urgency;status;fixed_version;debianbug;release;cve\n')

        for index, raw in enumerate(sorted_ip.iterrows()):  # we iterate over the sources (docker)
            source = raw[1]['source']
            source_version = raw[1]['source_version']
            release = dict_release['release_snapshot'][(source, source_version)]
            date_source = dict_date['date'][(source, source_version, release)]
            try:
                vuls = vulnerabilities[source]  # check if the source has any vulnerabilities
            except:
                continue
            for cve in vuls:  # for each vulnerability
                if not cve.startswith('CVE'):
                    continue
                v = vulnerabilities[source][cve]
                try:
                    status = v['releases'][release]['status']  # check only the release of source
                    urgency = v['releases'][release]['urgency']  # check only the release of source

                    try:
                        debianbug = str(v['debianbug'])
                    except:
                        debianbug = "undefined"

                    if status == "open" or status == "undetermined":  # if the vulnerability is still OPEN
                        fixed = "undefined"
                        fcsv.write(
                            source + ';' + source_version + ';' + urgency + ';' + status + ';' + fixed + ';' + debianbug + ';' + release + ';' + cve + '\n')
                    else:  # if the vulnerability is RESOLVED
                        try:
                            fixed = v['releases'][release]["fixed_version"]
                        except:
                            continue
                        if apt_pkg.version_compare(source_version,
                                                   fixed) < 0:  # Compare between the used source and fixed one (dates comparison)
                            fcsv.write(
                                source + ';' + source_version + ';' + urgency + ';' + status + ';' + fixed + ';' + debianbug + ';' + release + ';' + cve + '\n')

                except:
                    pass
        fcsv.close()

    def merge_vuls(self, tracked_packages):
        """Extracts and Merges vulnerabilities with tracked packages.
        :param tracked_packages: packages found installed in the container
        :return docker_vuls: knows vulnerabilities of the installed packages
        """
        self.final_vuls(tracked_packages)

        vuls = self.read_csv(VULS_CSV)  # GET VULNERABILITIES

        docker_vuls = (
            tracked_packages.
                set_index(['source', 'source_version']).
                merge(vuls.
                      set_index(['source', 'source_version']),
                      left_index=True,
                      right_index=True,
                      how='outer').dropna().reset_index().drop_duplicates()
        )
        return docker_vuls

    def connexion_udd(self):
        """Connects to the UDD (Ultimate Debian Database)
        """
        conn_string = "host='udd-mirror.debian.net' port='5432' dbname='udd' user='udd-mirror' password='udd-mirror'"
        conn = psycopg2.connect(conn_string)
        conn.set_client_encoding('UTF8')
        cursor = conn.cursor()
        return cursor

    def extract_bugs_from_udd(self, tracked_packages):
        """Extract knows bugs from UDD ( Ultimate Debian Database), for all packages found in the container.
        It extracts bugs for all versions and save them in a file.
        :param tracked_packages: packages found installed in the container
        """

        cursor = self.connexion_udd()

        unique_packages = tracked_packages.groupby('source').count().loc[:, []].reset_index()

        f = open(self.data_dir + BUGS_CSV, 'w')
        f.write('source;debianbug;found_in;fixed_in;type;status;severity;arrival;last_modified\n')

        for index, raw in enumerate(unique_packages.iterrows()):
            source = raw[1]['source']

            cursor.execute(
                "SELECT DISTINCT bugs.id, bugs.status, bugs.severity, " +
                "bugs.arrival, bugs.last_modified, bugs_found_in.version, bugs_fixed_in.version " +
                "FROM bugs_found_in, bugs LEFT JOIN bugs_fixed_in " +
                "ON bugs.id=bugs_fixed_in.id " +
                "WHERE bugs.id=bugs_found_in.id " +
                "AND bugs.source='" + source + "' ")
            data = cursor.fetchall()
            for x in data:
                id, status, severity, arrival, last_modified, found_in, fixed_in = x
                f.write(source + ';' + str(id) + ';' + found_in + ';' + str(
                    fixed_in) + ';normal;' + status + ';' + severity + ';' + str(arrival) + ';' + str(
                    last_modified) + '\n')

            cursor.execute(
                "SELECT DISTINCT archived_bugs.id, archived_bugs.status, archived_bugs.severity, " +
                "archived_bugs.arrival, archived_bugs.last_modified, " +
                "archived_bugs_found_in.version, archived_bugs_fixed_in.version " +
                "FROM archived_bugs_found_in, archived_bugs LEFT JOIN archived_bugs_fixed_in " +
                "ON archived_bugs.id=archived_bugs_fixed_in.id " +
                "WHERE archived_bugs.id=archived_bugs_found_in.id " +
                "AND archived_bugs.source='" + source + "'")
            data2 = cursor.fetchall()
            for x in data2:
                id, status, severity, arrival, last_modified, found_in, fixed_in = x
                f.write(source + ';' + str(id) + ';' + found_in + ';' + str(
                    fixed_in) + ';archived;' + status + ';' + severity + ';' + str(arrival) + ';' + str(
                    last_modified) + '\n')

        f.close()

    def get_bugs(self,tracked_packages):
        """Track bugs of the installed package versions: version where bug found <= version used < version where the bug was fixed
        :param tracked_packages: packages found installed in the container
        :return bugs: knows bugs of the installed packages
        """

        self.extract_bugs_from_udd(tracked_packages)
        bugs = self.read_csv(BUGS_CSV)

        bugs['fixed_in'] = bugs['fixed_in'].apply(lambda x: str(x).split('/')[-1])
        bugs['found_in'] = bugs['found_in'].apply(lambda x: str(x).split('/')[-1])

        sources = tracked_packages.groupby(['source', 'source_version', 'release_snapshot', 'date']).count().loc[:,
                  []].reset_index()
        deb_packages = self.read_csv(PACKAGES)

        deb_packages['source'] = deb_packages['source'].apply(str)
        sources['source'] = sources['source'].apply(str)
        bugs['source'] = bugs['source'].apply(str)

        bugs = (bugs.
                set_index(['source']).
                merge(sources.
                      set_index(['source']),
                      left_index=True,
                      right_index=True,
                      how='left').dropna().reset_index()
                )

        bugs = (bugs.
                set_index(['source', 'found_in']).
                merge(deb_packages.
                      rename(columns={'date': 'date_found', 'source_version': 'found_in'}).
                      set_index(['source', 'found_in']),
                      left_index=True,
                      right_index=True,
                      how='left').dropna().reset_index().drop_duplicates()
                )

        bugs['filtre'] = bugs.apply(
            lambda row: True if apt_pkg.version_compare(str(row['found_in']), str(row['source_version'])) <= 0
            else False, axis=1)

        bugs = bugs.query('filtre==True')  # date_found<=date and

        bugs = (bugs.
                set_index(['source', 'fixed_in']).
                merge(deb_packages.
                      rename(columns={'date': 'date_fixed', 'source_version': 'fixed_in'}).
                      set_index(['source', 'fixed_in']),
                      left_index=True,
                      right_index=True,
                      how='left').fillna('undefined').reset_index().drop_duplicates()
                )
        deb_packages == self.read_csv(PACKAGES)
        bugs['filtre'] = bugs.apply(
            lambda row: True if apt_pkg.version_compare(str(row['source_version']), str(row['fixed_in'])) < 0
            else False, axis=1)

        bugs = bugs.query('filtre==True')  # date_fixed>date

        bugs = bugs.groupby(['debianbug', 'source']).first().reset_index()

        return bugs

    def general_information(self):
        """inspect information from the Docker api
        :return results: general information about the analyzed Docker container
        """

        if ':' in self.image:
            slug = self.image.split(':')[0]
            tag = str(self.image.split(':')[1])
        else:
            slug = self.image
            tag = 'latest'

        if '/' in slug:
            url = 'https://registry.hub.docker.com/v2/repositories/' + slug
            slug_info = requests.get(url=url).json()
            url = 'https://registry.hub.docker.com/v2/repositories/' + slug + '/tags/' + tag
            tag_info = requests.get(url=url).json()

        else:
            url = 'https://registry.hub.docker.com/v2/repositories/library/' + slug
            slug_info = requests.get(url=url).json()
            url = 'https://registry.hub.docker.com/v2/repositories/library/' + slug + '/tags/' + tag
            tag_info = requests.get(url=url).json()

        keys = ['description', 'star_count', 'pull_count', 'full_size', 'last_updated', 'architectures']
        results = {}
        for key in keys:
            try:
                results[key] = str(tag_info[key])
            except:
                try:
                    results[key] = str(slug_info[key])
                except:
                    pass

        return results

    def remove_files(self):
        """Remove created files
        """
        os.system('rm {0}{1}'.format(self.data_dir, BUGS_CSV))
        os.system('rm {0}{1}'.format(self.data_dir, VULS_CSV))
