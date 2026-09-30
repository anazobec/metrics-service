-- Seed realistic, disposable AWX data for local collector development.
--
-- Run with:
--   podman exec -i postgres psql -U awx -d awx < tools/seed-awx-demo.sql
--
-- The seed covers the four completed UTC hours before the script runs. It is
-- intentionally separate from AWX migrations and safe to run more than once.

DO $$
DECLARE
    demo_org_id integer;
    demo_inventory_id integer;
    demo_instance_uuid text;
    demo_ee_id integer;
    demo_credential_type_id integer;
    demo_credential_id integer;
    demo_label_id integer;
    demo_jobtemplate_ctype_id integer;
    demo_workflow_ctype_id integer;
    demo_workflow_template_id integer;
    job_id integer;
    host_id integer;
    event_number integer;
    hour_index integer;
    job_index integer;
    host_index integer;
    job_created timestamptz;
    job_started timestamptz;
    job_finished timestamptz;
    job_status text;
    job_launch_type text;
    job_name text;
    event_time timestamptz;
    event_name text;
    event_data text;
    partition_name text;
    partition_start timestamptz;
    partition_end timestamptz;
BEGIN
    IF EXISTS (SELECT 1 FROM main_unifiedjob WHERE name = 'metrics-demo-hour-0-job-0') THEN
        RAISE NOTICE 'metrics demo data already exists; nothing to do';
        RETURN;
    END IF;

    -- Shared objects used by jobs and inventory collectors.
    INSERT INTO main_organization (created, modified, description, name, max_hosts)
    VALUES (now(), now(), 'Local metrics collector demo data', 'metrics-demo-organization', 1000)
    ON CONFLICT (name) DO UPDATE SET modified = EXCLUDED.modified
    RETURNING id INTO demo_org_id;

    INSERT INTO main_inventory (
        created, modified, description, name, variables, has_active_failures,
        total_hosts, hosts_with_active_failures, total_groups, has_inventory_sources,
        total_inventory_sources, inventory_sources_with_failures, organization_id,
        kind, pending_deletion, prevent_instance_group_fallback
    )
    VALUES (
        now(), now(), 'Local metrics collector demo inventory', 'metrics-demo-inventory', '{}', false,
        0, 0, 0, false, 0, 0, demo_org_id, '', false, false
    )
    ON CONFLICT (name, organization_id) DO UPDATE SET modified = EXCLUDED.modified
    RETURNING id INTO demo_inventory_id;

    demo_instance_uuid := 'metrics-demo-instance';
    INSERT INTO main_instance (
        uuid, hostname, created, modified, capacity, version, capacity_adjustment,
        cpu, memory, cpu_capacity, mem_capacity, enabled, managed_by_policy,
        ip_address, node_type, last_seen, errors, last_health_check,
        node_state, health_check_started, managed
    )
    VALUES (
        demo_instance_uuid, 'metrics-demo-controller', now(), now(), 100, '1.0', 1.0,
        4.0, 8589934592, 4, 8192, true, false, '192.0.2.10', 'hybrid',
        now(), '', now(), 'running', now(), true
    )
    ON CONFLICT (hostname) DO UPDATE SET modified = EXCLUDED.modified, last_seen = EXCLUDED.last_seen;

    FOR host_index IN 1..6 LOOP
        INSERT INTO main_host (
            created, modified, description, name, enabled, instance_id, variables,
            inventory_id, ansible_facts, ansible_facts_modified
        )
        VALUES (
            now(), now(), 'Demo managed host', format('metrics-demo-host-%s', host_index), true,
            demo_instance_uuid,
            jsonb_build_object(
                'ansible_host', format('192.0.2.%s', 20 + host_index),
                'ansible_connection', 'ssh',
                'ansible_port', 22
            )::text,
            demo_inventory_id,
            jsonb_build_object(
                'ansible_product_serial', format('DEMO-%s', host_index),
                'ansible_machine_id', format('metrics-demo-machine-%s', host_index),
                'ansible_virtualization_type', 'docker',
                'ansible_system_vendor', 'Demo Vendor',
                'ansible_product_name', 'Demo Linux host',
                'ansible_architecture', 'x86_64',
                'ansible_processor', 'Demo CPU'
            ),
            now()
        )
        ON CONFLICT (name, inventory_id) DO UPDATE SET modified = EXCLUDED.modified, enabled = true;
    END LOOP;

    INSERT INTO main_executionenvironment (
        created, modified, description, image, managed, name, pull,
        organization_id
    )
    VALUES (
        now(), now(), 'Local demo execution environment',
        'quay.io/ansible/awx-ee:latest', false, 'metrics-demo-ee', 'missing', demo_org_id
    )
    ON CONFLICT (name) DO UPDATE SET modified = EXCLUDED.modified
    RETURNING id INTO demo_ee_id;

    INSERT INTO django_content_type (app_label, model)
    VALUES ('main', 'jobtemplate')
    ON CONFLICT (app_label, model) DO UPDATE SET model = EXCLUDED.model
    RETURNING id INTO demo_jobtemplate_ctype_id;

    INSERT INTO django_content_type (app_label, model)
    VALUES ('main', 'workflowjobtemplate')
    ON CONFLICT (app_label, model) DO UPDATE SET model = EXCLUDED.model
    RETURNING id INTO demo_workflow_ctype_id;

    -- The local fixture's original job template predates content-type metadata.
    -- Completing it lets unified_job_template_table return a useful row.
    UPDATE main_unifiedjobtemplate
    SET polymorphic_ctype_id = demo_jobtemplate_ctype_id,
        organization_id = demo_org_id,
        execution_environment_id = demo_ee_id,
        modified = now()
    WHERE id = 1;

    INSERT INTO main_credentialtype (
        created, modified, description, name, kind, managed, inputs, injectors, namespace
    )
    VALUES (
        now(), now(), 'Credential type for local collector demo data',
        'Metrics Demo Machine', 'ssh', true, '{}', '{}', NULL
    )
    ON CONFLICT (name, kind) DO UPDATE SET modified = EXCLUDED.modified
    RETURNING id INTO demo_credential_type_id;

    INSERT INTO main_credential (
        created, modified, description, name, organization_id, inputs, credential_type_id, managed
    )
    VALUES (
        now(), now(), 'Disposable credential metadata only', 'metrics-demo-credential',
        demo_org_id, '{"username":"demo"}', demo_credential_type_id, false
    )
    ON CONFLICT (organization_id, name, credential_type_id) DO UPDATE SET modified = EXCLUDED.modified
    RETURNING id INTO demo_credential_id;

    INSERT INTO main_label (created, modified, description, name, organization_id)
    VALUES (now(), now(), 'Label used by the collector demo', 'metrics-demo', demo_org_id)
    ON CONFLICT (name, organization_id) DO UPDATE SET modified = EXCLUDED.modified
    RETURNING id INTO demo_label_id;

    INSERT INTO main_unifiedjobtemplate (
        created, modified, description, name, old_pk, last_job_failed, status,
        polymorphic_ctype_id, organization_id, execution_environment_id, org_unique
    )
    VALUES (
        now(), now(), 'Workflow template for collector demo data', 'metrics-demo-workflow',
        0, false, 'never updated', demo_workflow_ctype_id, demo_org_id, demo_ee_id, false
    )
    RETURNING id INTO demo_workflow_template_id;

    INSERT INTO main_workflowjobtemplate (
        unifiedjobtemplate_ptr_id, extra_vars, survey_enabled, survey_spec,
        allow_simultaneous, ask_variables_on_launch, ask_inventory_on_launch,
        inventory_id, ask_limit_on_launch, ask_scm_branch_on_launch, char_prompts,
        webhook_key, webhook_service, ask_labels_on_launch, ask_skip_tags_on_launch,
        ask_tags_on_launch
    )
    VALUES (
        demo_workflow_template_id, '{}', false, '{}', false, false, false,
        demo_inventory_id, false, false, '{}', '', '', false, false, false
    );

    INSERT INTO main_workflowjobtemplatenode (
        created, modified, unified_job_template_id, workflow_job_template_id,
        char_prompts, inventory_id, extra_data, survey_passwords,
        all_parents_must_converge, identifier, execution_environment_id
    )
    VALUES (
        now(), now(), 1, demo_workflow_template_id, '{}', demo_inventory_id,
        '{}', '{}', true, 'metrics-demo-workflow-node', demo_ee_id
    );

    -- Four completed hours, three jobs per hour, with mixed statuses and launch types.
    FOR hour_index IN 0..3 LOOP
        job_created := date_trunc('hour', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                       - make_interval(hours => 4 - hour_index);

        -- main_jobevent is partitioned by job_created. Create the partition before inserts.
        partition_name := format('main_jobevent_%s', to_char(job_created, 'YYYYMMDD_HH24'));
        partition_start := job_created;
        partition_end := job_created + interval '1 hour';
        IF to_regclass(format('public.%I', partition_name)) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE public.%I (LIKE public.main_jobevent INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
                partition_name
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1
            FROM pg_inherits
            WHERE inhparent = 'public.main_jobevent'::regclass
              AND inhrelid = to_regclass(format('public.%I', partition_name))
        ) THEN
            EXECUTE format(
                'ALTER TABLE public.main_jobevent ATTACH PARTITION public.%I FOR VALUES FROM (%L) TO (%L)',
                partition_name, partition_start, partition_end
            );
        END IF;

        FOR job_index IN 0..2 LOOP
            job_created := partition_start + make_interval(mins => 5 + job_index * 12);
            job_started := job_created + interval '2 minutes';
            job_finished := job_started + make_interval(mins => 8 + job_index * 4);
            job_status := CASE WHEN job_index = 1 THEN 'failed' ELSE 'successful' END;
            job_launch_type := CASE WHEN job_index = 1 THEN 'scheduled' ELSE 'manual' END;
            job_name := format('metrics-demo-hour-%s-job-%s', hour_index, job_index);

            INSERT INTO main_unifiedjob (
                created, modified, description, name, old_pk, launch_type, cancel_flag,
                status, failed, started, finished, elapsed, job_args, job_cwd,
                job_explanation, start_args, result_traceback, celery_task_id,
                unified_job_template_id, execution_node, emitted_events, controller_node,
                dependencies_processed, installed_collections, ansible_version,
                task_impact, job_env, polymorphic_ctype_id, organization_id,
                execution_environment_id
            )
            VALUES (
                job_created, job_finished, 'Metrics collector demo job', job_name, 0,
                job_launch_type, false, job_status, job_status = 'failed', job_started,
                job_finished, extract(epoch FROM job_finished - job_started), '{}', '/runner',
                CASE WHEN job_status = 'failed' THEN 'Demo failure for status distribution' ELSE '' END,
                '{}', CASE WHEN job_status = 'failed' THEN 'demo traceback' ELSE '' END,
                gen_random_uuid()::text, 1, 'metrics-demo-controller', 3,
                'metrics-demo-controller', true, '{"demo":true}', '2.16.0',
                1, '{"demo":true}',
                (SELECT id FROM django_content_type WHERE app_label = 'main' AND model = 'job'),
                demo_org_id, demo_ee_id
            )
            RETURNING id INTO job_id;

            INSERT INTO main_job (
                unifiedjob_ptr_id, job_type, playbook, forks, "limit", verbosity,
                extra_vars, job_tags, force_handlers, skip_tags, start_at_task,
                become_enabled, inventory_id, job_template_id, project_id,
                allow_simultaneous, artifacts, timeout, scm_revision, use_fact_cache,
                diff_mode, job_slice_count, job_slice_number, scm_branch,
                webhook_guid, webhook_service, survey_passwords, event_queries_processed
            )
            VALUES (
                job_id, 'run', 'site.yml', 5, '', 0, '{}', '', false, '', '', false,
                demo_inventory_id, 1, 1, false, '{}', 0, '', false, false, 1, 0, '',
                gen_random_uuid()::text, '', '{}', false
            );

            INSERT INTO main_unifiedjob_credentials (unifiedjob_id, credential_id)
            VALUES (job_id, demo_credential_id);
            INSERT INTO main_unifiedjob_labels (unifiedjob_id, label_id)
            VALUES (job_id, demo_label_id);

            host_index := 0;
            FOR host_id IN
                SELECT id FROM main_host WHERE inventory_id = demo_inventory_id ORDER BY id LIMIT 6
            LOOP
                host_index := host_index + 1;
                INSERT INTO main_jobhostsummary (
                    created, modified, host_name, changed, dark, failures, ok, processed,
                    skipped, failed, host_id, job_id, ignored, rescued
                )
                SELECT
                    job_started, job_finished, h.name,
                    CASE WHEN job_index = 2 THEN 1 ELSE 0 END,
                    0, CASE WHEN job_status = 'failed' AND host_index = 3 THEN 1 ELSE 0 END,
                    CASE WHEN job_status = 'failed' AND host_index = 3 THEN 0 ELSE 1 END,
                    1, 0, job_status = 'failed' AND host_index = 3,
                    h.id, job_id, 0, 0
                FROM main_host h
                WHERE h.id = host_id;

                event_number := 0;
                FOREACH event_name IN ARRAY ARRAY['runner_on_start', 'runner_on_ok', 'playbook_on_stats'] LOOP
                    event_number := event_number + 1;
                    event_time := job_started + make_interval(secs => event_number * 10 + host_index);
                    event_data := jsonb_build_object(
                        'task_action', CASE WHEN event_number = 1 THEN 'ansible.builtin.setup' ELSE 'ansible.builtin.debug' END,
                        'task_uuid', format('%s-%s-%s', job_id, host_id, event_number),
                        'duration', '1.2',
                        'start', event_time - interval '1 second',
                        'end', event_time,
                        'res', jsonb_build_object(
                            'changed', event_number = 2,
                            'warnings', jsonb_build_array(),
                            'deprecations', jsonb_build_array()
                        )
                    )::text;
                    INSERT INTO main_jobevent (
                        created, modified, event, event_data, failed, changed, host_name,
                        play, role, task, counter, host_id, job_id, uuid, parent_uuid,
                        end_line, playbook, start_line, stdout, verbosity, job_created
                    )
                    SELECT
                        event_time, event_time, event_name, event_data,
                        job_status = 'failed' AND event_number = 2,
                        event_number = 2, h.name, 'site', 'demo',
                        CASE WHEN event_number = 1 THEN 'Gather facts' ELSE 'Apply demo task' END,
                        event_number, h.id, job_id, gen_random_uuid()::text, '',
                        event_number, 'site.yml', event_number, format('%s on %s', event_name, h.name), 0,
                        partition_start
                    FROM main_host h
                    WHERE h.id = host_id;
                END LOOP;
            END LOOP;

            INSERT INTO main_workflowjobnode (
                created, modified, job_id, unified_job_template_id, workflow_job_id,
                inventory_id, ancestor_artifacts, extra_data, do_not_run,
                all_parents_must_converge, identifier, char_prompts, survey_passwords
            )
            VALUES (
                job_started, job_finished, job_id, 1, NULL, demo_inventory_id,
                '{}', '{}', false, true, format('metrics-demo-node-%s', job_id), '{}', '{}'
            );
        END LOOP;
    END LOOP;

    UPDATE main_inventory
    SET modified = now(), total_hosts = (SELECT count(*) FROM main_host WHERE inventory_id = demo_inventory_id)
    WHERE id = demo_inventory_id;

    -- Host-metric and monthly rows make the daily/snapshot collectors useful too.
    FOR host_index IN 1..6 LOOP
        INSERT INTO main_hostmetric (
            hostname, first_automation, last_automation, last_deleted,
            automated_counter, deleted_counter, deleted, used_in_inventories
        )
        VALUES (
            format('metrics-demo-host-%s', host_index),
            now() - interval '30 days', now() - interval '2 days', NULL,
            12 + host_index, 0, false, 1
        )
        ON CONFLICT (hostname) DO UPDATE SET
            last_automation = EXCLUDED.last_automation,
            automated_counter = EXCLUDED.automated_counter,
            deleted = false;
    END LOOP;

    FOR hour_index IN 1..3 LOOP
        INSERT INTO main_hostmetricsummarymonthly (
            date, license_consumed, license_capacity, hosts_added, hosts_deleted, indirectly_managed_hosts
        )
        VALUES (
            (current_date - make_interval(months => hour_index))::date,
            20 + hour_index, 100, 6, hour_index - 1, 0
        )
        ON CONFLICT (date) DO UPDATE SET
            license_consumed = EXCLUDED.license_consumed,
            hosts_added = EXCLUDED.hosts_added;
    END LOOP;

    RAISE NOTICE 'seeded metrics demo data for four completed UTC hours';
END
$$;
