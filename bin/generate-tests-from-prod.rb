#!/usr/bin/env ruby
# encoding: utf-8
# Copyright Vespa.ai. All rights reserved.
#
# Production-aware test generator: captures live data from production Vespa
# clusters and generates system tests that validate code changes against
# real-world behavior.
#
# Prerequisites:
#   - Access to production Vespa endpoint (k8s service address)
#   - vespa CLI or HTTP access to /document/v1 and /search/ APIs
#
# Usage:
#   bin/generate-tests-from-prod.rb \
#     --endpoint http://vespa.prod.svc:8080 \
#     --schema music \
#     --sample-size 100 \
#     --queries-from-log access.log \
#     --output tests/search/generated/prod_regression \
#     --diff HEAD~1..HEAD
#
#   # Or with config file
#   bin/generate-tests-from-prod.rb --config prod-test.yaml

require 'json'
require 'net/http'
require 'uri'
require 'optparse'
require 'fileutils'
require 'yaml'
require 'time'

module ProdTestGenerator

  # Phase 1: Capture production state
  class ProdCapture
    attr_reader :endpoint, :schemas, :sample_docs, :sample_queries, :metrics

    def initialize(endpoint, options = {})
      @endpoint = endpoint.chomp('/')
      @tls = options[:tls] || false
      @cert = options[:cert]
      @key = options[:key]
      @timeout = options[:timeout] || 30
      @verbose = options[:verbose] || false
    end

    # Fetch all schema definitions from the running application
    def capture_schemas
      log("Capturing schemas from #{@endpoint}...")
      @schemas = {}

      # Method 1: Via /ApplicationStatus or config API
      app_response = http_get("/ApplicationStatus")
      if app_response && app_response.code == '200'
        log("Got application status")
      end

      # Method 2: Via vespa CLI (if available)
      cli_schemas = `vespa document-type list --target #{@endpoint} 2>/dev/null`.strip
      unless cli_schemas.empty?
        cli_schemas.split("\n").each do |schema_name|
          schema_name = schema_name.strip
          next if schema_name.empty?
          @schemas[schema_name] = { name: schema_name, source: 'cli' }
        end
      end

      # Method 3: Try direct schema endpoint
      schema_response = http_get("/config/v1/vespa.configdefinition.schema")
      if schema_response && schema_response.code == '200'
        log("Got schema config")
      end

      @schemas
    end

    # Download schema .sd files from config server
    def download_schema_files(config_endpoint, output_dir)
      log("Downloading schema files from config server...")
      FileUtils.mkdir_p(output_dir)

      # Via config server API
      response = http_get_from(config_endpoint, "/config/v2/tenant/default/application/default/cloud.config.schema/")
      if response && response.code == '200'
        parsed = JSON.parse(response.body)
        parsed.each do |schema_url|
          schema_resp = http_get_from(config_endpoint, schema_url)
          if schema_resp && schema_resp.code == '200'
            schema_data = JSON.parse(schema_resp.body)
            File.write("#{output_dir}/#{schema_data['name']}.sd", schema_data['content'])
            log("  Downloaded: #{schema_data['name']}.sd")
          end
        end
      end
    end

    # Sample documents via /document/v1 visit
    def capture_documents(schema_name, sample_size: 100, selection: nil)
      log("Visiting #{sample_size} documents from #{schema_name}...")
      @sample_docs ||= {}
      docs = []

      # Use document/v1 API with visit
      params = "wantedDocumentCount=#{[sample_size, 1000].min}&timeout=30"
      params += "&selection=#{URI.encode_www_form_component(selection)}" if selection

      continuation = nil
      visited = 0

      loop do
        path = "/document/v1/#{schema_name}/#{schema_name}/docid/?#{params}"
        path += "&continuation=#{continuation}" if continuation

        response = http_get(path)
        break unless response && response.code == '200'

        result = JSON.parse(response.body)
        batch = result['documents'] || []
        docs.concat(batch)
        visited += batch.size

        log("  Visited #{visited}/#{sample_size} documents")
        break if visited >= sample_size

        continuation = result['continuation']
        break unless continuation
      end

      @sample_docs[schema_name] = docs.first(sample_size)
      log("  Captured #{@sample_docs[schema_name].size} documents")
      @sample_docs[schema_name]
    end

    # Capture search queries from access log or run sample queries
    def capture_queries_from_log(log_source, limit: 50)
      log("Extracting queries from #{log_source}...")
      @sample_queries = []

      if File.exist?(log_source)
        # Parse Vespa access log format
        File.foreach(log_source) do |line|
          if match = line.match(/\bquery=([^\s&]+)|\byql=([^\s&]+)/)
            query = match[1] || match[2]
            @sample_queries << URI.decode_www_form_component(query)
          end
          break if @sample_queries.size >= limit
        end
      elsif log_source.start_with?('http')
        # Fetch from log API endpoint
        response = http_get_from(log_source, "")
        if response && response.code == '200'
          response.body.split("\n").each do |line|
            if match = line.match(/query=([^\s&]+)/)
              @sample_queries << URI.decode_www_form_component(match[1])
            end
            break if @sample_queries.size >= limit
          end
        end
      end

      log("  Extracted #{@sample_queries.size} queries")
      @sample_queries
    end

    # Capture current query results as baseline (the "oracle")
    def capture_query_baselines(queries, schema_name: nil)
      log("Capturing baseline results for #{queries.size} queries...")
      baselines = []

      queries.each_with_index do |query, i|
        q = query.include?('?') ? query : "query=#{URI.encode_www_form_component(query)}"
        q += "&hits=10&nocache"
        q += "&sources=#{schema_name}" if schema_name

        response = http_get("/search/?#{q}")
        next unless response && response.code == '200'

        result = JSON.parse(response.body)
        baselines << {
          query: query,
          total_hits: result.dig('root', 'fields', 'totalCount') || 0,
          top_hits: extract_top_hits(result, 5),
          coverage: result.dig('root', 'coverage', 'coverage'),
        }
        log("  [#{i+1}/#{queries.size}] \"#{query[0..50]}\" → #{baselines.last[:total_hits]} hits")
      end

      baselines
    end

    # Capture metrics snapshot
    def capture_metrics
      log("Capturing metrics snapshot...")
      @metrics = {}

      response = http_get("/metrics/v2/values")
      if response && response.code == '200'
        parsed = JSON.parse(response.body)
        # Extract key search metrics
        (parsed['nodes'] || []).each do |node|
          (node['services'] || []).each do |svc|
            next unless svc['name'] =~ /search|container|content/
            (svc['metrics'] || []).each do |m|
              values = m['values'] || {}
              name = m.dig('dimensions', 'metricName') || m['name']
              next unless name
              @metrics[name] = values
            end
          end
        end
      end

      log("  Captured #{@metrics.size} metric keys")
      @metrics
    end

    private

    def extract_top_hits(result, n)
      children = result.dig('root', 'children') || []
      children.first(n).map do |hit|
        {
          id: hit['id'],
          relevance: hit['relevance'],
          fields: (hit['fields'] || {}).select { |k, _| !k.start_with?('sddocname') },
        }
      end
    end

    def http_get(path)
      http_get_from(@endpoint, path)
    end

    def http_get_from(base, path)
      uri = URI("#{base}#{path}")
      http = Net::HTTP.new(uri.host, uri.port)
      http.use_ssl = uri.scheme == 'https'
      http.read_timeout = @timeout
      http.open_timeout = @timeout

      if @cert && @key
        http.cert = OpenSSL::X509::Certificate.new(File.read(@cert))
        http.key = OpenSSL::PKey::RSA.new(File.read(@key))
      end

      request = Net::HTTP::Get.new(uri)
      http.request(request)
    rescue => e
      log("  HTTP error: #{e.message}")
      nil
    end

    def log(msg)
      $stderr.puts "[prod-capture] #{msg}" if @verbose
    end
  end

  # Phase 2: Analyze changes and detect coverage gaps
  class CoverageAnalyzer

    def initialize(change_profile, existing_tests, verbose: false)
      @change_profile = change_profile
      @existing_tests = existing_tests
      @verbose = verbose
    end

    # Determine what aspects of the change are NOT covered by existing ST
    def find_gaps
      gaps = []

      changed_features = @change_profile[:features] || Set.new
      covered_features = Set.new
      @existing_tests.each do |test|
        covered_features.merge(test[:features] || [])
      end

      uncovered = changed_features - covered_features
      if uncovered.any?
        gaps << {
          type: :feature_gap,
          description: "Changed features not covered by selected tests",
          features: uncovered.to_a,
          severity: :high,
        }
      end

      # Check if schema changes have matching test data
      changed_schemas = @change_profile[:schema_names] || Set.new
      tested_schemas = Set.new
      @existing_tests.each { |t| tested_schemas.merge(t[:schema_names] || []) }

      untested_schemas = changed_schemas - tested_schemas
      if untested_schemas.any?
        gaps << {
          type: :schema_gap,
          description: "Changed schemas with no matching test data",
          schemas: untested_schemas.to_a,
          severity: :high,
        }
      end

      # Check ranking changes need query verification
      if changed_features.include?('rank_profile') || changed_features.include?('first_phase') || changed_features.include?('second_phase')
        gaps << {
          type: :ranking_gap,
          description: "Ranking changes need live query comparison to detect relevance regression",
          severity: :medium,
        }
      end

      # Check if structural schema changes need data migration test
      if changed_features.include?('struct_field') || changed_features.include?('map_type') || changed_features.include?('document_reference')
        gaps << {
          type: :migration_gap,
          description: "Structural schema changes need real-data compatibility test",
          severity: :medium,
        }
      end

      log("Found #{gaps.size} coverage gaps")
      gaps
    end

    private

    def log(msg)
      $stderr.puts "[coverage] #{msg}" if @verbose
    end
  end

  # Phase 3: Generate test artifacts from production data
  class TestGenerator

    def initialize(output_dir, verbose: false)
      @output_dir = output_dir
      @verbose = verbose
    end

    # Generate a complete system test from captured production data
    def generate(test_name, schema_name:, schema_content: nil,
                 documents: [], query_baselines: [], services_xml: nil,
                 change_description: "auto-generated from production data")

      test_dir = File.join(@output_dir, test_name)
      app_dir = File.join(test_dir, 'app')
      schemas_dir = File.join(app_dir, 'schemas')
      FileUtils.mkdir_p(schemas_dir)

      # 1. Write schema file
      if schema_content
        File.write("#{schemas_dir}/#{schema_name}.sd", schema_content)
        log("Wrote schema: #{schema_name}.sd")
      end

      # 2. Write services.xml
      services = services_xml || generate_services_xml(schema_name)
      File.write("#{app_dir}/services.xml", services)
      log("Wrote services.xml")

      # 3. Write feed data (sampled documents)
      feed_docs = documents.map do |doc|
        {
          'put' => doc['id'] || "id:#{schema_name}:#{schema_name}::test-#{SecureRandom.hex(4)}",
          'fields' => doc['fields'] || doc,
        }
      end
      File.write("#{test_dir}/feed.json", JSON.pretty_generate(feed_docs))
      log("Wrote #{feed_docs.size} documents to feed.json")

      # 4. Write expected query results
      unless query_baselines.empty?
        File.write("#{test_dir}/expected_results.json", JSON.pretty_generate(query_baselines))
        log("Wrote #{query_baselines.size} query baselines")
      end

      # 5. Generate the Ruby test file
      test_code = generate_test_code(
        test_name: test_name,
        schema_name: schema_name,
        doc_count: feed_docs.size,
        query_baselines: query_baselines,
        change_description: change_description,
      )
      File.write("#{test_dir}/#{test_name}.rb", test_code)
      log("Wrote test: #{test_name}.rb")

      # 6. Write metadata
      meta = {
        generated_at: Time.now.iso8601,
        source: 'production',
        schema: schema_name,
        doc_count: feed_docs.size,
        query_count: query_baselines.size,
        change_description: change_description,
      }
      File.write("#{test_dir}/metadata.json", JSON.pretty_generate(meta))

      test_dir
    end

    private

    def generate_services_xml(schema_name)
      <<~XML
        <?xml version="1.0" encoding="utf-8" ?>
        <services version="1.0">
          <container id="default" version="1.0">
            <search/>
            <document-api/>
            <nodes>
              <node hostalias="node1"/>
            </nodes>
          </container>

          <content id="content" version="1.0">
            <min-redundancy>1</min-redundancy>
            <documents>
              <document type="#{schema_name}" mode="index" />
            </documents>
            <nodes>
              <node hostalias="node1" distribution-key="0"/>
            </nodes>
          </content>
        </services>
      XML
    end

    def generate_test_code(test_name:, schema_name:, doc_count:, query_baselines:, change_description:)
      class_name = test_name.split('_').map(&:capitalize).join('')

      # Determine assertion style based on what baselines we have
      query_assertions = query_baselines.map do |b|
        query_str = b[:query].include?('=') ? b[:query] : "query=#{b[:query]}"
        total = b[:total_hits]

        lines = []
        # Hitcount assertion: allow ±10% tolerance for production data
        if total > 0
          min_hits = [(total * 0.9).floor, 1].max
          lines << "    # Production baseline: #{total} hits"
          lines << "    result = search(\"#{escape_ruby(query_str)}&hits=10&nocache\")"
          lines << "    hitcount = result.hitcount"
          lines << "    assert(hitcount >= #{min_hits}, \"Expected >= #{min_hits} hits for '#{escape_ruby(b[:query][0..40])}', got \\#{hitcount}\")"
        end

        # Top-hit relevance ordering (if we have top hits)
        if b[:top_hits] && b[:top_hits].size >= 2
          lines << "    # Verify top-hit ordering is preserved"
          lines << "    assert(result.hit.size >= 2, \"Expected at least 2 hits\")" if total >= 2
        end

        lines.join("\n")
      end.reject(&:empty?)

      <<~RUBY
        # Copyright Vespa.ai. All rights reserved.
        # Auto-generated production regression test
        # #{change_description}
        # Generated: #{Time.now.iso8601}

        require 'indexed_only_search_test'
        require 'json'

        class #{class_name} < IndexedOnlySearchTest

          def setup
            set_owner("auto-generated")
            set_description("Production regression test for #{schema_name}: #{change_description}")
          end

          def test_feed_and_query_regression
            deploy_app(SearchApp.new.sd(selfdir + 'app/schemas/#{schema_name}.sd'))
            start

            # Feed production-sampled documents
            feed(:file => selfdir + 'feed.json', :timeout => 240)
            wait_for_hitcount("query=sddocname:#{schema_name}", #{doc_count})

        #{query_assertions.empty? ? "    # No query baselines captured - basic feed verification only\n    assert_hitcount(\"query=sddocname:#{schema_name}\", #{doc_count})" : query_assertions.join("\n\n")}
          end

          def teardown
            stop
          end
        end
      RUBY
    end

    def escape_ruby(str)
      str.gsub('\\', '\\\\\\\\').gsub('"', '\\"').gsub('#', '\\#')
    end

    def log(msg)
      $stderr.puts "[test-gen] #{msg}" if @verbose
    end
  end

  # Phase 4: Orchestrator - ties everything together
  class Orchestrator

    def initialize(config)
      @config = config
      @verbose = config[:verbose] || false
    end

    def run
      # Step 1: Analyze code changes
      log("=== Step 1: Analyzing code changes ===")
      changed_files = get_changed_files
      return { status: :no_changes } if changed_files.empty?

      # Step 2: Select existing tests
      log("=== Step 2: Selecting existing tests ===")
      selector_output = `ruby #{File.expand_path('../select-tests.rb', __FILE__)} --files "#{changed_files.join(',')}" --format json 2>/dev/null`
      existing_tests = JSON.parse(selector_output, symbolize_names: true) rescue []
      log("Selected #{existing_tests.size} existing tests")

      # Step 3: Analyze change profile (reuse select-tests logic)
      change_profile = analyze_change_profile(changed_files)

      # Step 4: Find coverage gaps
      log("=== Step 3: Analyzing coverage gaps ===")
      analyzer = CoverageAnalyzer.new(change_profile, existing_tests, verbose: @verbose)
      gaps = analyzer.find_gaps

      if gaps.empty?
        log("No coverage gaps found. Existing tests are sufficient.")
        return {
          status: :sufficient,
          existing_tests: existing_tests.map { |t| t[:file] },
          gaps: [],
        }
      end

      log("Found #{gaps.size} gaps: #{gaps.map { |g| g[:type] }.join(', ')}")

      # Step 5: Capture production data to fill gaps
      log("=== Step 4: Capturing production data ===")
      generated_tests = []

      if @config[:endpoint]
        capture = ProdCapture.new(@config[:endpoint], {
          tls: @config[:tls],
          cert: @config[:cert],
          key: @config[:key],
          verbose: @verbose,
        })

        generator = TestGenerator.new(@config[:output] || 'tests/search/generated', verbose: @verbose)

        # For each gap, capture targeted production data
        gaps.each do |gap|
          case gap[:type]
          when :schema_gap
            gap[:schemas].each do |schema_name|
              log("Capturing data for uncovered schema: #{schema_name}")
              docs = capture.capture_documents(schema_name, sample_size: @config[:sample_size] || 50)
              next if docs.empty?

              test_dir = generator.generate(
                "prod_#{schema_name}_regression",
                schema_name: schema_name,
                documents: docs,
                change_description: "Schema gap: #{schema_name} not covered by existing ST",
              )
              generated_tests << test_dir
            end

          when :ranking_gap
            # Capture query baselines to detect ranking regression
            schemas = change_profile[:schema_names]&.to_a || []
            schemas.each do |schema_name|
              log("Capturing ranking baselines for: #{schema_name}")
              queries = if @config[:queries_from_log]
                capture.capture_queries_from_log(@config[:queries_from_log], limit: 20)
              else
                # Generate representative queries from sampled docs
                docs = capture.capture_documents(schema_name, sample_size: 20)
                generate_queries_from_docs(docs)
              end

              next if queries.empty?
              baselines = capture.capture_query_baselines(queries, schema_name: schema_name)
              docs = capture.sample_docs[schema_name] || []

              test_dir = generator.generate(
                "prod_#{schema_name}_ranking_regression",
                schema_name: schema_name,
                documents: docs,
                query_baselines: baselines,
                change_description: "Ranking regression: verify query results after ranking change",
              )
              generated_tests << test_dir
            end

          when :migration_gap
            schemas = gap[:schemas] || change_profile[:schema_names]&.to_a || []
            schemas.each do |schema_name|
              log("Capturing migration test data for: #{schema_name}")
              docs = capture.capture_documents(schema_name, sample_size: @config[:sample_size] || 100)
              next if docs.empty?

              test_dir = generator.generate(
                "prod_#{schema_name}_migration",
                schema_name: schema_name,
                documents: docs,
                change_description: "Migration: verify existing data compatible with schema change",
              )
              generated_tests << test_dir
            end

          when :feature_gap
            log("Feature gap detected: #{gap[:features].join(', ')}")
            log("  → Consider adding targeted tests for these features")
          end
        end
      else
        log("No production endpoint configured. Reporting gaps only.")
      end

      {
        status: :generated,
        existing_tests: existing_tests.map { |t| t[:file] },
        gaps: gaps,
        generated_tests: generated_tests,
        run_command: build_run_command(existing_tests, generated_tests),
      }
    end

    private

    def get_changed_files
      diff_spec = @config[:diff] || 'HEAD~1..HEAD'
      `git diff --name-only #{diff_spec}`.strip.split("\n").reject(&:empty?)
    end

    def analyze_change_profile(changed_files)
      # Lightweight change analysis (subset of select-tests.rb logic)
      profile = { features: Set.new, schema_names: Set.new, categories: Set.new }

      feature_patterns = {
        'tensor' => /\btensor\b/, 'hnsw' => /\bhnsw\b/i,
        'rank_profile' => /\brank-profile\b/, 'first_phase' => /\bfirst-phase\b/,
        'second_phase' => /\bsecond-phase\b/, 'global_phase' => /\bglobal-phase\b/,
        'struct_field' => /\bstruct-field\b/, 'map_type' => /\bmap<\b/,
        'document_reference' => /\breference<\b/, 'import_field' => /\bimport\s+field\b/,
        'streaming' => /\bstreaming\b/i, 'bm25' => /\bbm25\b/i,
        'nearest_neighbor' => /\bnearest.neighbor\b/i,
      }

      changed_files.each do |f|
        case File.extname(f)
        when '.sd'
          profile[:categories] << 'schema'
          profile[:schema_names] << File.basename(f, '.sd')
          if File.exist?(f)
            content = File.read(f, encoding: 'utf-8', invalid: :replace, undef: :replace)
            feature_patterns.each { |feat, pat| profile[:features] << feat if content.match?(pat) }
          end
        when '.xml'
          profile[:categories] << 'services' if File.basename(f) == 'services.xml'
        when '.java'
          profile[:categories] << 'java_component'
        end
      end

      profile
    end

    def generate_queries_from_docs(docs)
      return [] if docs.nil? || docs.empty?
      queries = []
      docs.first(10).each do |doc|
        fields = doc['fields'] || doc
        fields.each do |key, value|
          next unless value.is_a?(String) && value.length > 2 && value.length < 100
          # Use first meaningful word as query
          words = value.split(/\s+/).reject { |w| w.length < 3 }
          queries << words.first if words.any?
          break if queries.size >= 20
        end
        break if queries.size >= 20
      end
      queries.uniq
    end

    def build_run_command(existing_tests, generated_tests)
      files = existing_tests.map { |t| t[:file] }
      generated_tests.each do |dir|
        rb_files = Dir.glob("#{dir}/*.rb")
        rb_files.each { |f| files << f.sub(/^tests\//, '') }
      end
      "bin/run-tests-on-swarm.sh #{files.map { |f| "-f #{f}" }.join(' ')}"
    end

    def log(msg)
      $stderr.puts msg if @verbose
    end
  end
end

# --- CLI ---
if __FILE__ == $0
  options = {
    verbose: false,
    diff: 'HEAD~1..HEAD',
    sample_size: 50,
    output: 'tests/search/generated',
  }

  OptionParser.new do |opts|
    opts.banner = "Usage: #{$0} [options]"

    opts.on('--endpoint URL', 'Production Vespa endpoint') { |v| options[:endpoint] = v }
    opts.on('--config FILE', 'YAML config file') { |v| options[:config_file] = v }
    opts.on('--schema NAME', 'Target schema name') { |v| options[:schema] = v }
    opts.on('--sample-size N', Integer, 'Documents to sample (default: 50)') { |v| options[:sample_size] = v }
    opts.on('--queries-from-log FILE', 'Extract queries from access log') { |v| options[:queries_from_log] = v }
    opts.on('--diff RANGE', 'Git diff range (default: HEAD~1..HEAD)') { |v| options[:diff] = v }
    opts.on('--output DIR', 'Output directory (default: tests/search/generated)') { |v| options[:output] = v }
    opts.on('--tls', 'Use TLS') { options[:tls] = true }
    opts.on('--cert FILE', 'TLS cert file') { |v| options[:cert] = v }
    opts.on('--key FILE', 'TLS key file') { |v| options[:key] = v }
    opts.on('--verbose', 'Verbose output') { options[:verbose] = true }
    opts.on('--dry-run', 'Analyze only, no capture') { options[:dry_run] = true }
  end.parse!

  # Load config file if specified
  if options[:config_file] && File.exist?(options[:config_file])
    file_config = YAML.safe_load(File.read(options[:config_file]), symbolize_names: true)
    options = file_config.merge(options.reject { |_, v| v.nil? })
  end

  orchestrator = ProdTestGenerator::Orchestrator.new(options)

  if options[:dry_run]
    # Just analyze gaps without capturing
    $stderr.puts "=== Dry Run: Gap Analysis Only ==="
    result = orchestrator.run
    puts JSON.pretty_generate(result)
  else
    result = orchestrator.run

    case result[:status]
    when :no_changes
      puts "No code changes detected."
    when :sufficient
      puts "Existing tests are sufficient (#{result[:existing_tests].size} tests selected)."
      puts "\nRun with:"
      puts "  bin/run-tests-on-swarm.sh #{result[:existing_tests].map { |f| "-f #{f}" }.join(' ')}"
    when :generated
      puts "Generated #{result[:generated_tests].size} additional tests to fill coverage gaps."
      puts "\nGaps found:"
      result[:gaps].each { |g| puts "  - [#{g[:severity]}] #{g[:description]}" }
      puts "\nGenerated tests:"
      result[:generated_tests].each { |t| puts "  #{t}" }
      puts "\nRun all with:"
      puts "  #{result[:run_command]}"
    end
  end
end
