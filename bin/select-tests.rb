#!/usr/bin/env ruby
# encoding: utf-8
# Copyright Vespa.ai. All rights reserved.
#
# Dynamic test selector: analyzes git changes and selects relevant system tests.
#
# Usage:
#   bin/select-tests.rb                        # changes vs HEAD~1
#   bin/select-tests.rb --diff HEAD~3..HEAD    # changes in last 3 commits
#   bin/select-tests.rb --diff main..feature   # branch diff
#   bin/select-tests.rb --files a.sd b.xml     # explicit changed files
#   bin/select-tests.rb --verbose              # show matching reasons
#   bin/select-tests.rb --top 20               # limit output count
#   bin/select-tests.rb --format runtest       # output as runtest.sh args

require 'set'
require 'optparse'
require 'json'
require 'pathname'

class TestSelector
  TESTS_DIR = File.expand_path('../../tests', __FILE__)

  # Vespa feature keywords extracted from schema/config files
  FEATURE_PATTERNS = {
    # Schema features
    'tensor'             => /\btensor\b/,
    'hnsw'               => /\bhnsw\b/i,
    'nearest_neighbor'   => /\bnearest.neighbor\b/i,
    'embedding'          => /\bembed\b|\bembedding\b/i,
    'bm25'               => /\bbm25\b/i,
    'rank_profile'       => /\brank-profile\b/,
    'first_phase'        => /\bfirst-phase\b/,
    'second_phase'       => /\bsecond-phase\b/,
    'global_phase'       => /\bglobal-phase\b/,
    'struct_field'       => /\bstruct-field\b/,
    'map_type'           => /\bmap<\b/,
    'array_type'         => /\barray<\b/,
    'document_reference'  => /\breference<\b/,
    'import_field'       => /\bimport\s+field\b/,
    'fieldset'           => /\bfieldset\b/,
    'stemming'           => /\bstemming\b/,
    'bolding'            => /\bbolding\b/,
    'summary'            => /\bsummary\b/,
    'attribute'          => /\battribute\b/,
    'index'              => /\bindex\b/,
    'fast_search'        => /\bfast-search\b/,
    'streaming'          => /\bstreaming\b/i,
    'document_processor' => /\bdocument-processing\b/,
    'searcher'           => /\bsearcher\b|\bsearch-chain\b/,
    'handler'            => /\bhandler\b/,
    'model_evaluation'   => /\bmodel-evaluation\b/,
    'onnx'               => /\bonnx\b/i,
    'feed'               => /\bfeeding\b|\bfeed\b/,
    'redundancy'         => /\bredundancy\b/,
    'group'              => /\bgroup\b.*\bnode\b/,
    'tls'                => /\btls\b|\bcertificate\b/i,
    'acl'                => /\bacl\b/i,
  }.freeze

  # Map of broad change categories to test directory patterns
  CATEGORY_MAP = {
    'schema'    => %w[search],
    'services'  => %w[config container search vds],
    'docproc'   => %w[docproc],
    'container' => %w[container],
    'ranking'   => %w[search],
    'feeding'   => %w[search vds],
    'config'    => %w[config],
    'vds'       => %w[vds],
  }.freeze

  def initialize(options = {})
    @verbose = options[:verbose] || false
    @top = options[:top] || 0
    @format = options[:format] || 'list'
  end

  # Main entry: select tests based on changed files
  def select(changed_files)
    return [] if changed_files.empty?

    # Step 1: Classify the changes
    change_profile = analyze_changes(changed_files)
    log("Change profile: #{JSON.pretty_generate(change_profile)}")

    # Step 2: Build test index (lazy, cached)
    test_index = build_test_index

    # Step 3: Score each test against the change profile
    scored = score_tests(test_index, change_profile, changed_files)

    # Step 4: Sort by score descending, apply limit
    results = scored.sort_by { |t| -t[:score] }.reject { |t| t[:score] <= 0 }
    results = results.first(@top) if @top > 0

    results
  end

  private

  def log(msg)
    $stderr.puts "[select-tests] #{msg}" if @verbose
  end

  # Analyze changed files to produce a change profile
  def analyze_changes(changed_files)
    profile = {
      categories: Set.new,
      features: Set.new,
      changed_dirs: Set.new,
      file_types: Set.new,
      schema_names: Set.new,
      keywords: Set.new,
    }

    changed_files.each do |f|
      ext = File.extname(f)
      profile[:file_types] << ext

      # Track changed directories relative to tests/
      if f.start_with?('tests/')
        parts = f.sub('tests/', '').split('/')
        profile[:changed_dirs] << parts[0..1].join('/') if parts.length >= 2
      end

      case ext
      when '.sd'
        profile[:categories] << 'schema'
        # Extract schema name from filename
        schema_name = File.basename(f, '.sd')
        profile[:schema_names] << schema_name
        extract_features_from_file(f, profile)

      when '.xml'
        if File.basename(f) == 'services.xml'
          profile[:categories] << 'services'
          extract_features_from_file(f, profile)
        elsif File.basename(f) == 'hosts.xml'
          profile[:categories] << 'config'
        end

      when '.rb'
        profile[:categories] << 'test_code'
        # If a test file itself changed, it should definitely be selected
        if f.start_with?('tests/')
          profile[:changed_dirs] << f.sub('tests/', '').split('/')[0..1].join('/')
        end

      when '.java'
        profile[:categories] << 'java_component'
        extract_java_category(f, profile)

      when '.py'
        profile[:categories] << 'python_component'

      when '.json'
        profile[:categories] << 'data' if f.include?('feed') || f.include?('doc')
      end
    end

    profile
  end

  def extract_features_from_file(filepath, profile)
    full_path = resolve_path(filepath)
    return unless File.exist?(full_path)

    content = File.read(full_path, encoding: 'utf-8', invalid: :replace, undef: :replace)
    FEATURE_PATTERNS.each do |feature, pattern|
      if content.match?(pattern)
        profile[:features] << feature
        profile[:keywords] << feature
      end
    end
  end

  def extract_java_category(filepath, profile)
    if filepath.include?('docproc') || filepath.include?('DocumentProcessor')
      profile[:categories] << 'docproc'
    end
    if filepath.include?('searcher') || filepath.include?('Searcher')
      profile[:categories] << 'container'
    end
    if filepath.include?('handler') || filepath.include?('Handler')
      profile[:categories] << 'container'
    end
  end

  def resolve_path(filepath)
    abs = File.expand_path(filepath, File.expand_path('../..', __FILE__))
    File.exist?(abs) ? abs : filepath
  end

  # Build an index of all tests with their feature fingerprints
  def build_test_index
    cache_file = File.join(TESTS_DIR, '..', '.test-index-cache.json')

    # Use cache if fresh (< 1 hour old)
    if File.exist?(cache_file) && (Time.now - File.mtime(cache_file)) < 3600
      log("Using cached test index from #{cache_file}")
      return JSON.parse(File.read(cache_file), symbolize_names: true)
    end

    log("Building test index from #{TESTS_DIR}...")
    index = []

    Dir.glob("#{TESTS_DIR}/**/*.rb").each do |test_file|
      next unless File.read(test_file, encoding: 'utf-8', invalid: :replace, undef: :replace).include?('def test')
      relative = test_file.sub("#{TESTS_DIR}/", '')
      parts = relative.split('/')
      category = parts[0]
      test_dir = parts[0..1].join('/')

      entry = {
        file: relative,
        category: category,
        test_dir: test_dir,
        features: Set.new,
        schema_names: Set.new,
        app_features: Set.new,
      }

      # Scan the test's app directory for schema and config features
      app_dir = File.join(File.dirname(test_file), 'app')
      if Dir.exist?(app_dir)
        scan_app_dir(app_dir, entry)
      end

      # Also scan sibling .sd files (some tests have .sd at the same level)
      Dir.glob(File.join(File.dirname(test_file), '*.sd')).each do |sd_file|
        scan_sd_file(sd_file, entry)
      end

      # Scan the test .rb file itself for keywords
      scan_test_file(test_file, entry)

      # Convert sets to arrays for JSON serialization
      entry[:features] = entry[:features].to_a
      entry[:schema_names] = entry[:schema_names].to_a
      entry[:app_features] = entry[:app_features].to_a

      index << entry
    end

    # Cache the index
    begin
      File.write(cache_file, JSON.pretty_generate(index))
      log("Cached test index with #{index.size} tests")
    rescue => e
      log("Warning: could not cache index: #{e.message}")
    end

    index
  end

  def scan_app_dir(app_dir, entry)
    Dir.glob("#{app_dir}/**/*.sd").each do |sd_file|
      scan_sd_file(sd_file, entry)
    end
    Dir.glob("#{app_dir}/**/schemas/*.sd").each do |sd_file|
      scan_sd_file(sd_file, entry)
    end
    Dir.glob("#{app_dir}/**/services.xml").each do |xml_file|
      scan_config_file(xml_file, entry)
    end
  end

  def scan_sd_file(sd_file, entry)
    content = File.read(sd_file, encoding: 'utf-8', invalid: :replace, undef: :replace)
    entry[:schema_names] << File.basename(sd_file, '.sd')
    FEATURE_PATTERNS.each do |feature, pattern|
      entry[:features] << feature if content.match?(pattern)
    end
  end

  def scan_config_file(xml_file, entry)
    content = File.read(xml_file, encoding: 'utf-8', invalid: :replace, undef: :replace)
    FEATURE_PATTERNS.each do |feature, pattern|
      entry[:app_features] << feature if content.match?(pattern)
    end
  end

  def scan_test_file(test_file, entry)
    content = File.read(test_file, encoding: 'utf-8', invalid: :replace, undef: :replace)
    content.encode!('UTF-8', invalid: :replace, undef: :replace, replace: '')
    # Look for deploy_app calls referencing schema files
    content.scan(/sd\([^)]*["']([^"']+)\.sd["']/).flatten.each do |name|
      entry[:schema_names] << File.basename(name)
    end
    # Look for feature-related method calls
    FEATURE_PATTERNS.each do |feature, pattern|
      entry[:features] << feature if content.match?(pattern)
    end
  end

  # Score each test against the change profile
  def score_tests(test_index, change_profile, changed_files)
    test_index.map do |test|
      score = 0
      reasons = []

      # Rule 1: Direct hit - changed file IS a test file or in same test dir
      changed_files.each do |cf|
        if cf.start_with?('tests/') && cf == "tests/#{test[:file]}"
          score += 100
          reasons << "direct_change"
        end
      end

      # Rule 2: Same test directory as a changed file
      change_profile[:changed_dirs].each do |cd|
        if test[:test_dir] == cd
          score += 80
          reasons << "same_dir:#{cd}"
        end
      end

      # Rule 3: Category match
      change_profile[:categories].each do |cat|
        dirs = CATEGORY_MAP[cat] || []
        if dirs.include?(test[:category])
          score += 10
          reasons << "category:#{cat}"
        end
      end

      # Rule 4: Feature overlap (most important for smart selection)
      test_features = Set.new(test[:features]) | Set.new(test[:app_features])
      common_features = test_features & change_profile[:features]
      if common_features.any?
        # Score proportional to overlap ratio
        overlap_score = (common_features.size.to_f / [change_profile[:features].size, 1].max * 40).round
        score += overlap_score
        reasons << "features:#{common_features.to_a.join(',')}" if overlap_score > 0
      end

      # Rule 5: Schema name match
      common_schemas = Set.new(test[:schema_names]) & change_profile[:schema_names]
      if common_schemas.any?
        score += 60
        reasons << "schema:#{common_schemas.to_a.join(',')}"
      end

      # Rule 6: If change is in lib/ (framework change), run a broad smoke set
      if changed_files.any? { |f| f.start_with?('lib/') }
        # Framework changes need broad coverage - boost "basic" tests
        if test[:test_dir].include?('basic') || test[:file].include?('smoke')
          score += 30
          reasons << "framework_smoke"
        end
      end

      {
        file: test[:file],
        score: score,
        reasons: reasons,
        category: test[:category],
        features: test[:features],
      }
    end
  end
end

# --- CLI ---

def get_changed_files_from_diff(diff_spec)
  cmd = "git diff --name-only #{diff_spec}"
  files = `#{cmd}`.strip.split("\n").reject(&:empty?)
  if $?.exitstatus != 0
    $stderr.puts "Error running: #{cmd}"
    exit 1
  end
  files
end

if __FILE__ == $0
  options = { verbose: false, top: 0, format: 'list', diff: 'HEAD~1..HEAD', files: [] }

  OptionParser.new do |opts|
    opts.banner = "Usage: #{$0} [options]"

    opts.on('--diff RANGE', 'Git diff range (default: HEAD~1..HEAD)') { |v| options[:diff] = v }
    opts.on('--files x,y,z', Array, 'Explicit list of changed files') { |v| options[:files] = v }
    opts.on('--verbose', 'Show matching reasons') { options[:verbose] = true }
    opts.on('--top N', Integer, 'Limit to top N tests') { |v| options[:top] = v }
    opts.on('--format FORMAT', %w[list json runtest], 'Output format (list/json/runtest)') { |v| options[:format] = v }
  end.parse!

  # Get changed files
  changed_files = if options[:files].any?
    options[:files]
  else
    get_changed_files_from_diff(options[:diff])
  end

  if changed_files.empty?
    $stderr.puts "No changed files detected."
    exit 0
  end

  $stderr.puts "Changed files (#{changed_files.size}):" if options[:verbose]
  changed_files.each { |f| $stderr.puts "  #{f}" } if options[:verbose]

  selector = TestSelector.new(verbose: options[:verbose], top: options[:top], format: options[:format])
  results = selector.select(changed_files)

  case options[:format]
  when 'json'
    puts JSON.pretty_generate(results)
  when 'runtest'
    # Output as -f arguments for run-tests-on-swarm.sh
    results.each { |r| print "-f #{r[:file]} " }
    puts
  else
    results.each do |r|
      line = "#{r[:file]}  (score: #{r[:score]})"
      line += "  [#{r[:reasons].join(', ')}]" if options[:verbose]
      puts line
    end
  end

  $stderr.puts "\nSelected #{results.size} tests from #{changed_files.size} changed files." if options[:verbose]
end
