import logging
import json
import urllib2
import traceback

from ckan.model import Session
from ckan.logic import get_action
from ckan import model

from ckanext.harvest.harvesters.base import HarvesterBase
from ckan.lib.munge import munge_tag
from ckan.lib.munge import munge_title_to_name
from ckanext.harvest.model import HarvestObject

import oaipmh.client
from oaipmh.metadata import MetadataRegistry

from metadata import oai_ddi_reader
from metadata import oai_dc_reader

log = logging.getLogger(__name__)


class OaipmhHarvester(HarvesterBase):
    '''
    OAI-PMH Harvester
    '''

    def info(self):
        '''
        Return information about this harvester.
        '''
        return {
            'name': 'oai_pmh',
            'title': 'OAI-PMH Harvester',
            'description': 'Harvester for OAI-PMH data sources'
        }

    def gather_stage(self, harvest_job):
        '''
        The gather stage will recieve a HarvestJob object and will be
        responsible for:
            - gathering all the necessary objects to fetch on a later.
              stage (e.g. for a CSW server, perform a GetRecords request)
            - creating the necessary HarvestObjects in the database, specifying
              the guid and a reference to its source and job.
            - creating and storing any suitable HarvestGatherErrors that may
              occur.
            - returning a list with all the ids of the created HarvestObjects.

        :param harvest_job: HarvestJob object
        :returns: A list of HarvestObject ids
        '''
        log.debug("in gather stage: %s" % harvest_job.source.url)
        try:
            harvest_obj_ids = []
            registry = self._create_metadata_registry()
            self._set_config(harvest_job.source.config)
            client = oaipmh.client.Client(
                harvest_job.source.url,
                registry,
                self.credentials,
                force_http_get=self.force_http_get
            )

            client.identify()  # check if identify works
            for header in self._identifier_generator(client):
                harvest_obj = HarvestObject(
                    guid=header.identifier(),
                    job=harvest_job
                )
                harvest_obj.save()
                harvest_obj_ids.append(harvest_obj.id)
                log.debug("Harvest obj %s created" % harvest_obj.id)
        except urllib2.HTTPError, e:
            log.exception(
                'Gather stage failed on %s (%s): %s, %s'
                % (
                    harvest_job.source.url,
                    e.fp.read(),
                    e.reason,
                    e.hdrs
                )
            )
            self._save_gather_error(
                'Could not gather anything from %s' %
                harvest_job.source.url, harvest_job
            )
            return None
        except Exception, e:
            log.exception(
                'Gather stage failed on %s: %s'
                % (
                    harvest_job.source.url,
                    str(e),
                )
            )
            self._save_gather_error(
                'Could not gather anything from %s: %s / %s'
                % (harvest_job.source.url, str(e), traceback.format_exc()),
                harvest_job
            )
            return None
        log.debug(
            "Gather stage successfully finished with %s harvest objects"
            % len(harvest_obj_ids)
        )
        return harvest_obj_ids

    def _identifier_generator(self, client):
        """
        pyoai generates the URL based on the given method parameters
        Therefore one may not use the set parameter if it is not there
        """
        if self.set_spec:
            for header in client.listIdentifiers(
                    metadataPrefix=self.md_format,
                    set=self.set_spec):
                yield header
        else:
            for header in client.listIdentifiers(
                    metadataPrefix=self.md_format):
                yield header

    def _create_metadata_registry(self):
        registry = MetadataRegistry()
        registry.registerReader('oai_dc', oai_dc_reader)
        registry.registerReader('oai_ddi', oai_ddi_reader)
        return registry

    def _set_config(self, source_config):
        try:
            config_json = json.loads(source_config)
            log.debug('config_json: %s' % config_json)
            try:
                username = config_json['username']
                password = config_json['password']
                self.credentials = (username, password)
            except (IndexError, KeyError):
                self.credentials = None

            self.user = 'harvest'
            self.set_spec = config_json.get('set', None)
            self.md_format = config_json.get('metadata_prefix', 'oai_dc')
            self.force_http_get = config_json.get('force_http_get', False)

        except ValueError:
            pass

    def fetch_stage(self, harvest_object):
        '''
        The fetch stage will receive a HarvestObject object and will be
        responsible for:
            - getting the contents of the remote object (e.g. for a CSW server,
              perform a GetRecordById request).
            - saving the content in the provided HarvestObject.
            - creating and storing any suitable HarvestObjectErrors that may
              occur.
            - returning True if everything went as expected, False otherwise.

        :param harvest_object: HarvestObject object
        :returns: True if everything went right, False if errors were found
        '''
        log.debug("in fetch stage: %s" % harvest_object.guid)
        try:
            self._set_config(harvest_object.job.source.config)
            registry = self._create_metadata_registry()
            client = oaipmh.client.Client(
                harvest_object.job.source.url,
                registry,
                self.credentials,
                force_http_get=self.force_http_get
            )
            record = None
            try:
                log.debug(
                    "Load %s with metadata prefix '%s'" %
                    (harvest_object.guid, self.md_format)
                )

                self._before_record_fetch(harvest_object)
                record = client.getRecord(
                    identifier=harvest_object.guid,
                    metadataPrefix=self.md_format
                )
                self._after_record_fetch(record)
                log.debug('record found!')
            except:
                log.exception('getRecord failed for %s' % harvest_object.guid)
                self._save_object_error(
                    'Get record failed for %s!' % harvest_object.guid,
                    harvest_object
                )
                return False

            header, metadata, _ = record
            log.debug('metadata %s' % metadata)
            log.debug('header %s' % header)

            try:
                metadata_modified = header.datestamp().isoformat()
            except:
                metadata_modified = None

            try:
                content_dict = metadata.getMap()
                content_dict['set_spec'] = header.setSpec()
                if metadata_modified:
                    content_dict['metadata_modified'] = metadata_modified
                log.debug(content_dict)
                content = json.dumps(content_dict)
            except:
                log.exception('Dumping the metadata failed!')
                self._save_object_error(
                    'Dumping the metadata failed!',
                    harvest_object
                )
                return False

            harvest_object.content = content
            harvest_object.save()
        except Exception, e:
            log.exception(e)
            self._save_object_error(
                (
                    'Exception in fetch stage for %s: %r / %s'
                    % (harvest_object.guid, e, traceback.format_exc())
                ),
                harvest_object
            )
            return False

        return True

    def _before_record_fetch(self, harvest_object):
        pass

    def _after_record_fetch(self, record):
        pass

    def import_stage(self, harvest_object):
        '''
        The import stage will receive a HarvestObject object and will be
        responsible for:
            - performing any necessary action with the fetched object (e.g
              create a CKAN package).
              Note: if this stage creates or updates a package, a reference
              to the package must be added to the HarvestObject.
              Additionally, the HarvestObject must be flagged as current.
            - creating the HarvestObject - Package relation (if necessary)
            - creating and storing any suitable HarvestObjectErrors that may
              occur.
            - returning True if everything went as expected, False otherwise.

        :param harvest_object: HarvestObject object
        :returns: True if everything went right, False if errors were found
        '''

        log.debug("in import stage: %s" % harvest_object.guid)
        if not harvest_object:
            log.error('No harvest object received')
            self._save_object_error('No harvest object received')
            return False

        try:
            self._set_config(harvest_object.job.source.config)
            context = {
                'model': model,
                'session': Session,
                'user': self.user,
                'ignore_auth': True,
            }

            package_dict = {}
            content = json.loads(harvest_object.content)
            log.debug(content)

            package_dict['id'] = munge_title_to_name(harvest_object.guid)
            package_dict['name'] = package_dict['id']

            mapping = self._get_mapping()

            for ckan_field, oai_field in mapping.iteritems():
                try:
                    package_dict[ckan_field] = content[oai_field][0]
                except (IndexError, KeyError):
                    continue

            # add author
            package_dict['author'] = self._extract_author(content)

            # add owner_org
            source_dataset = get_action('package_show')(
              context.copy(),
              {'id': harvest_object.source.id}
            )
            owner_org = source_dataset.get('owner_org')
            package_dict['owner_org'] = owner_org

            # add license
            package_dict['license_id'] = self._extract_license_id(content)

            # add resources
            url = self._get_possible_resource(harvest_object, content)
            package_dict['resources'] = self._extract_resources(url, content)

            # extract tags from 'type' and 'subject' field
            # everything else is added as extra field
            tags, extras = self._extract_tags_and_extras(content)
            package_dict['tags'] = tags
            if content['set_spec']:
                extras.append(('set_spec', ",".join(content['set_spec'])))
            package_dict['extras'] = extras

            # allow sub-classes to add additional fields
            package_dict = self._extract_additional_fields(
                content,
                package_dict
            )

            log.debug('Create/update package using dict: %s' % package_dict)
            self._create_or_update_package(
                package_dict,
                harvest_object
            )

            Session.commit()

            log.debug("Finished record")
        except Exception, e:
            log.exception(e)
            self._save_object_error(
                (
                    'Exception in fetch stage for %s: %r / %s'
                    % (harvest_object.guid, e, traceback.format_exc())
                ),
                harvest_object
            )
            return False
        return True

    def _get_mapping(self):
        return {
            'title': 'title',
            'notes': 'description',
            'maintainer': 'publisher',
            'maintainer_email': 'maintainer_email',
            'url': 'source',
        }

    def _extract_author(self, content):
        return ', '.join(content['creator'])

    def _extract_license_id(self, content):
        return ', '.join(content['rights'])

    def _extract_tags_and_extras(self, content):
        extras = []
        tags = []
        for key, value in content.iteritems():
            if key in self._get_mapping().values():
                continue
            if key in ['type', 'subject']:
                if type(value) is list:
                    tags.extend(value)
                else:
                    tags.extend(value.split(';'))
                continue
            if value and type(value) is list:
                value = value[0]
            if not value:
                value = None
            if key.endswith('date') and value:
                # the ckan indexer can't handle timezone-aware datetime objects
                try:
                    from dateutil.parser import parse
                    date_value = parse(value)
                    date_without_tz = date_value.replace(tzinfo=None)
                    value = date_without_tz.isoformat()
                except (ValueError, TypeError):
                    continue

            extras.append((key, value))

        tags = [munge_tag(tag[:100]) for tag in tags]

        return (tags, extras)

    def _get_possible_resource(self, harvest_obj, content):
        url = None
        candidates = content['identifier']
        candidates.append(harvest_obj.guid)
        for ident in candidates:
            if ident.startswith('http://') or ident.startswith('https://'):
                url = ident
                break
        return url

    def _extract_resources(self, url, content):
        resources = []
        log.debug('URL of ressource: %s' % url)
        if url:
            try:
                resource_format = content['format'][0]
            except (IndexError, KeyError):
                resource_format = 'HTML'
            resources.append({
                'name': content['title'][0],
                'resource_type': resource_format,
                'format': resource_format,
                'url': url
            })
        return resources

    def _extract_additional_fields(self, content, package_dict):
        # This method is the ideal place for sub-classes to
        # change whatever they want in the package_dict
        return package_dict
